"""ASE leases -> native LC3 routes. Control only; no USB, ISO loop or PCM.

Native receiver groups share a codec timeline/mixer/SRC for split stereo.
Microphone leases use their own namespace and epoch so HFP cleanup cannot
remove them. The native graph remains the final capture ownership authority.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
import inspect
import logging

from .endpoints import Token

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Member:
    token: Token
    lane: int
    link: object = None


@dataclass(eq=False)
class Sink:
    identity: tuple
    owner: object
    key: str
    device_key: str
    epoch: int
    codec: object
    split: bool
    allocation: int
    budget: object = None
    lease: int = 0
    prepared: bool = False
    closing: bool = False
    members: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(eq=False)
class Source:
    token: Token
    key: str
    device_key: str
    epoch: int
    codec: object
    budget: object = None
    lease: int = 0
    capture_epoch: int = 0
    prepared: bool = False
    closing: bool = False
    link: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    clock: object = None


class NativeStreamBridge:
    def __init__(
        self,
        client,
        *,
        input_endpoint=None,
        system_audio=False,
        system_endpoint=None,
        gains=None,
        protection=None,
        input_opener=None,
        emit=lambda *a, **k: None,
        media_changed=lambda *a, **k: None,
        source_changed=lambda *a, **k: None,
    ):
        self.client, self.control = client, None
        self.clock_device = None
        self.latency = None
        self.input_endpoint, self.system_audio = input_endpoint, bool(system_audio)
        self.system_endpoint = system_endpoint
        self.gains = dict(gains or {})
        self.render_gain, self.remote_muted = 1.0, False
        self.protection, self.input_opener = protection, input_opener
        self.emit, self.media_changed, self.source_changed = emit, media_changed, source_changed
        self.groups, self.routes = {}, {}
        self.source = None
        self.source_lock = asyncio.Lock()
        self.sequence = 0
        self.closed = False

    def attach(self, control):
        if self.control is not None and self.control is not control:
            raise RuntimeError("Native bridge is already attached to another profile")
        self.control = control

    def set_controller(self, device):
        self.clock_device = device

    def next_epoch(self):
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF or 1
        return self.sequence

    async def rpc(self, operation, **values):
        return await asyncio.to_thread(self.client.request, operation, **values)

    @staticmethod
    async def callback(function, *arguments):
        result = function(*arguments)
        if inspect.isawaitable(result):
            await result

    def current(self, token):
        return not self.closed and self.control is not None and self.control.current(token)

    def device_key(self, owner):
        peer = self.control.peer(owner)
        return str(getattr(owner, "peer_address", f"le-peer-{peer.identity}"))

    def parameters(self, record):
        return dict(key=record.key, lease=record.lease, epoch=record.epoch)

    def group_plan(self, token, codec, qos):
        peer = self.control.peer(token.owner)
        split = codec.channels == 1 and codec.allocation in (1, 2)
        identity = (peer.identity, "cig", qos.cig) if split else (peer.identity, "ase", token.ase)
        lane = 1 if split and codec.allocation == 2 else 0
        return identity, split, lane

    @staticmethod
    def parent_identity(owner):
        return dict(parent=owner.handle, parent_epoch=getattr(owner, "_bluehead_link_epoch", 0))

    @classmethod
    def link_identity(cls, link):
        return dict(
            **cls.parent_identity(link.acl_connection), link_epoch=getattr(link, "_bluehead_link_epoch", 0)
        )

    def down_gain(self, device_key):
        return self.gains.get(device_key, {}).get("down", 1.0) * self.render_gain

    def up_gain(self, device_key):
        return self.gains.get(device_key, {}).get("up", 1.0) * (not self.remote_muted)

    async def prepare(self, token, codec, qos):
        if not self.current(token):
            return
        if token in self.routes:
            return
        if token.source:
            await self.prepare_source(token, codec, qos)
            return
        plan = self.latency.plan(codec, qos.presentation_us) if self.latency else None
        identity, split, lane = self.group_plan(token, codec, qos)
        record = self.groups.get(identity)
        if record is not None:
            if record.closing:
                record = None
            elif (record.codec.rate, record.codec.duration_us, record.codec.octets, record.codec.blocks) != (
                codec.rate,
                codec.duration_us,
                codec.octets,
                codec.blocks,
            ):
                raise ValueError("Split stereo directions need matching LC3 frame configurations")
        if record is None:
            epoch = self.next_epoch()
            record = Sink(
                identity,
                token.owner,
                f"le:rx:{identity[0]}:{epoch}",
                self.device_key(token.owner),
                epoch,
                codec,
                split,
                codec.allocation,
            )
            self.groups[identity] = record
        if record.budget is not None and plan is not None and record.budget != plan:
            raise ValueError("Split channels require the same presentation budget")
        record.budget = plan
        member = Member(token, lane)
        self.routes[token] = record
        record.members[token] = member
        async with record.lock:
            if not self.current(token) or record.closing:
                return
            if not record.lease:
                parameters = {"buffer_us": record.budget.queue_us} if record.budget else {}
                record.lease = (await self.rpc("mix_add", key=record.key, **parameters))["lease"]
            if not record.prepared:
                await self.configure_sink(record)
            await self.rpc("mix_gain", key=record.key, lease=record.lease, gain=self.down_gain(record.device_key))

    async def configure_sink(self, record):
        codec = record.codec
        await self.rpc(
            "le_rx_prepare",
            **self.parameters(record),
            rate=codec.rate,
            duration_us=codec.duration_us,
            channels=2 if record.split else codec.channels,
            octets=codec.octets,
            blocks=codec.blocks,
            split=record.split,
            controller_timestamps=True,
            **self.parent_identity(record.owner),
            allocation=0 if record.split else record.allocation,
        )
        record.prepared = True

    async def prepare_source(self, token, codec, qos):
        async with self.source_lock:
            await self._prepare_source(token, codec, qos)

    async def _prepare_source(self, token, codec, qos):
        budget = self.latency.plan(codec, qos.presentation_us, source=True) if self.latency else None
        previous = self.source
        if previous is not None:
            if not self.current(previous.token):
                await self.stop(previous.token)
            if previous.closing:
                await asyncio.wait_for(previous.closed.wait(), 0.75)
            else:
                raise OSError("Another voice source already owns the microphone")
        if not self.current(token):
            return
        if self.protection is not None and not await self.protection.prepare(token.owner, "lc3"):
            raise OSError("Microphone protection could not retire competing connections")
        if not self.current(token):
            return
        epoch = self.next_epoch()
        record = Source(
            token,
            f"call:le:{self.control.peer(token.owner).identity}:{epoch}",
            self.device_key(token.owner),
            epoch,
            codec,
        )
        record.budget = budget
        self.source = record
        self.routes[token] = record
        async with record.lock:
            record.lease = (await self.rpc("mix_add", key=record.key))["lease"]
            await self.rpc(
                "le_tx_prepare",
                **self.parameters(record),
                **self.parent_identity(token.owner),
                **({"capture_buffer_us": record.budget.queue_us} if record.budget else {}),
                rate=codec.rate,
                duration_us=codec.duration_us,
                octets=codec.octets,
                blocks=codec.blocks,
            )
            record.prepared = True
            await self.rpc("le_tx_gain", **self.parameters(record), gain=self.up_gain(record.device_key))

    async def bind(self, token, link):
        record = self.routes.get(token)
        if record is None or not self.current(token):
            return
        if isinstance(record, Sink):
            async with record.lock:
                member = record.members.get(token)
                if member is None or not self.current(token) or record.closing:
                    return
                if member.link is link:
                    return
                await self.rpc(
                    "le_rx_bind",
                    **self.parameters(record),
                    handle=link.handle,
                    **self.link_identity(link),
                    lane=member.lane,
                )
                member.link = link
            await self.callback(self.media_changed, record, True)
            return
        async with record.lock:
            if not self.current(token) or record.closing or self.source is not record:
                return
            result = await self.rpc(
                "le_tx_bind", **self.parameters(record), handle=link.handle, **self.link_identity(link)
            )
            record.capture_epoch = result["voice_epoch"]
            record.link = link
            if self.protection is not None and not self.protection.activate(token.owner, link, "lc3"):
                raise OSError("Microphone protection reservation was retired")
        # Endpoint preparation is cancellable/epoch-checked by the native actor.
        # Do not hold a record lock while Windows opens a microphone; Stop and
        # another peer's media control must remain able to retire their leases.
        if self.input_opener is not None:
            await self.input_opener(record)
        else:
            await asyncio.to_thread(
                self.client.audio, "input", self.input_endpoint, voice_epoch=record.capture_epoch
            )
        if not self.current(token) or record.closing:
            return
        if self.system_audio:
            await self.enable_system(record, True)
        if not self.current(token) or record.closing or self.source is not record:
            return
        if self.clock_device is not None:
            from .clock_feedback import ClockFeedback

            record.clock = ClockFeedback(self.clock_device, failure=getattr(self, "failure", lambda error: None))
            record.clock.start(link)
        await self.callback(self.source_changed, record, True)

    async def enable_system(self, record, enabled):
        ticket = await self.rpc("system_audio", enabled=enabled, voice_epoch=record.capture_epoch,
                                endpoint_id=self.system_endpoint or "")
        await asyncio.to_thread(self.client.wait_audio, ticket)

    async def stop(self, token):
        record = self.routes.pop(token, None)
        if record is None:
            return
        if isinstance(record, Source):
            record.closing = True
            try:
                async with record.lock:
                    if record.prepared:
                        with contextlib.suppress(OSError):
                            await self.rpc("le_tx_stop", **self.parameters(record))
                    if record.lease:
                        with contextlib.suppress(OSError):
                            await self.rpc("mix_remove", key=record.key, lease=record.lease)
                if record.clock is not None:
                    await record.clock.close()
                if self.protection is not None:
                    if record.link is None:
                        self.protection.failed(token.owner)
                    else:
                        self.protection.ended(token.owner, record.link)
                if record.capture_epoch:
                    await self.callback(self.source_changed, record, False)
            finally:
                if self.source is record:
                    self.source = None
                record.closed.set()
            return
        async with record.lock:
            record.members.pop(token, None)
            remaining = [member for member in record.members.values() if self.current(member.token)]
            if remaining:
                # A deliberate ASE topology change is a new decoder generation.
                # Ordinary radio loss uses per-channel PLC and never rebuilds.
                if record.prepared:
                    await self.rpc("le_rx_stop", **self.parameters(record))
                record.epoch = self.next_epoch()
                await self.configure_sink(record)
                for member in remaining:
                    if member.link is not None:
                        await self.rpc(
                            "le_rx_bind",
                            **self.parameters(record),
                            handle=member.link.handle,
                            **self.link_identity(member.link),
                            lane=member.lane,
                        )
            else:
                record.closing = True
                if self.groups.get(record.identity) is record:
                    self.groups.pop(record.identity)
                if record.prepared:
                    with contextlib.suppress(OSError):
                        await self.rpc("le_rx_stop", **self.parameters(record))
                if record.lease:
                    with contextlib.suppress(OSError):
                        await self.rpc("mix_remove", key=record.key, lease=record.lease)
        await self.callback(self.media_changed, record, bool(remaining))

    async def set_input(self, endpoint):
        previous, self.input_endpoint = self.input_endpoint, endpoint
        record = self.source
        try:
            if record is not None and record.capture_epoch and not record.closing:
                await asyncio.to_thread(self.client.audio, "input", endpoint, voice_epoch=record.capture_epoch)
        except Exception:
            if self.source is record and not record.closing:
                self.input_endpoint = previous
                raise

    async def set_system(self, enabled):
        record = self.source
        if record is not None and record.capture_epoch and not record.closing:
            await self.enable_system(record, enabled)
        self.system_audio = bool(enabled)

    async def set_system_source(self, endpoint):
        record = self.source
        if record is not None and record.capture_epoch and not record.closing and self.system_audio:
            await asyncio.to_thread(self.client.audio, "system", endpoint, voice_epoch=record.capture_epoch)
        self.system_endpoint = endpoint

    async def set_gain(self, device_key, direction, gain):
        if direction not in ("down", "up") or type(gain) not in (int, float) or not 0 <= gain <= 2:
            raise ValueError("Invalid per-device LE Audio gain")
        self.gains.setdefault(device_key, {})[direction] = gain
        if direction == "down":
            for record in tuple(self.groups.values()):
                if record.device_key == device_key and record.lease and not record.closing:
                    await self.rpc("mix_gain", key=record.key, lease=record.lease, gain=self.down_gain(device_key))
        else:
            record = self.source
            if record is not None and record.device_key == device_key and record.prepared and not record.closing:
                await self.rpc("le_tx_gain", **self.parameters(record), gain=self.up_gain(device_key))

    async def set_render_gain(self, gain):
        if type(gain) not in (int, float) or not 0 <= gain <= 1:
            raise ValueError("Invalid LE renderer volume")
        self.render_gain = float(gain)
        for record in tuple(self.groups.values()):
            if record.lease and not record.closing:
                await self.rpc(
                    "mix_gain", key=record.key, lease=record.lease, gain=self.down_gain(record.device_key)
                )

    async def set_remote_mute(self, muted):
        self.remote_muted = bool(muted)
        record = self.source
        if record is not None and record.prepared and not record.closing:
            await self.rpc("le_tx_gain", **self.parameters(record), gain=self.up_gain(record.device_key))

    async def snapshot(self, owner):
        media = []
        for record in tuple(self.groups.values()):
            if record.owner is owner and record.prepared and not record.closing:
                media.append(await self.rpc("le_rx_snapshot", **self.parameters(record)))
        record = self.source
        microphone = (
            await self.rpc("le_tx_snapshot", **self.parameters(record))
            if record is not None and record.token.owner is owner and record.prepared and not record.closing
            else {}
        )
        return dict(media=media, microphone=microphone)

    async def close(self):
        self.closed = True
        for token in tuple(self.routes):
            await self.stop(token)
