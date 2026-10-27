"""Encrypted BASS Scan Delegator; bounded source slots and explicit state."""

import asyncio
from dataclasses import dataclass, field
import inspect

from bumble import att, gatt
from bumble.profiles import bass
from .bass_control import parse_operation


@dataclass(eq=False)
class Source:
    identity: int
    address: object
    sid: int
    broadcast: int
    pa_sync: int
    interval: int
    subgroups: tuple
    owner: object
    pa_state: int = 0
    encryption: int = 0
    actual_masks: tuple = ()
    actual_metadata: tuple = ()
    code: bytes | None = field(default=None, repr=False)
    bad_code: bytes = field(default=b"", repr=False)
    session: int = 0

    def value(self):
        groups = [bass.SubgroupInfo(mask, meta) for mask, meta in zip(self.actual_masks, self.actual_metadata)]
        if not groups:
            groups = [bass.SubgroupInfo(0, group.metadata) for group in self.subgroups]
        return bytes(
            bass.BroadcastReceiveState(
                self.identity,
                self.address,
                self.sid,
                self.broadcast,
                self.pa_state,
                self.encryption,
                self.bad_code,
                groups,
            )
        )


class BroadcastScanService(gatt.TemplateService):
    UUID = gatt.GATT_BROADCAST_AUDIO_SCAN_SERVICE

    def __init__(self, device, receiver, *, slots=4):
        if not 1 <= slots <= 4:
            raise ValueError("Invalid broadcast source slot count")
        self.device, self.receiver = device, receiver
        self.admit = lambda owner: True
        self.sources = [None] * slots
        self.serial, self.pending, self.closed = 0, 0, False
        self.lock = asyncio.Lock()
        self.clients, self.tasks = {}, set()
        self.previous_callback = receiver.on_changed
        receiver.on_changed = self.state_changed
        self.states = [
            gatt.Characteristic(
                gatt.GATT_BROADCAST_RECEIVE_STATE_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ | gatt.Characteristic.Properties.NOTIFY,
                gatt.Characteristic.Permissions.READABLE
                | gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
                gatt.CharacteristicValue(read=lambda owner, index=index: self.read(owner, index)),
            )
            for index in range(slots)
        ]
        self.control_point = gatt.Characteristic(
            gatt.GATT_BROADCAST_AUDIO_SCAN_CONTROL_POINT_CHARACTERISTIC,
            gatt.Characteristic.Properties.WRITE | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            gatt.Characteristic.Permissions.WRITEABLE | gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION,
            gatt.CharacteristicValue(write=self.write),
        )
        super().__init__([self.control_point, *self.states])

    def secured(self, owner):
        if self.closed or not self.admit(owner) or self.device.connections.get(owner.handle) is not owner:
            raise att.ATT_Error(att.ATT_UNLIKELY_ERROR_ERROR)
        if not getattr(owner, "encryption", False):
            raise att.ATT_Error(att.ATT_INSUFFICIENT_ENCRYPTION_ERROR)
        if id(owner) not in self.clients:
            if len(self.clients) >= 3:
                raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR)

            def callback(reason):
                self.disconnected(owner)

            self.clients[id(owner)] = (owner, callback)
            owner.on("disconnection", callback)

    def disconnected(self, owner):
        entry = self.clients.pop(id(owner), None)
        if entry is not None:
            owner.remove_listener("disconnection", entry[1])
        for source in self.sources:
            if source is not None and source.owner is owner:
                source.owner = None

    def read(self, owner, index):
        self.secured(owner)
        source = self.sources[index]
        return source.value() if source is not None else b""

    async def notify(self, index):
        for owner, callback in tuple(self.clients.values()):
            if self.device.connections.get(owner.handle) is owner and getattr(owner, "encryption", False):
                source = self.sources[index]
                await self.device.notify_subscriber(owner, self.states[index], source.value() if source else b"")

    def find(self, identity):
        for index, source in enumerate(self.sources):
            if source is not None and source.identity == identity:
                return index, source
        raise att.ATT_Error(0x81)

    async def deactivate_other_sources(self, keep):
        for index, source in enumerate(self.sources):
            if source is not None and source is not keep and source.session:
                source.session = 0
                source.pa_state = 0
                source.actual_masks = ()
                source.actual_metadata = ()
                await self.notify(index)

    async def connect(self, source):
        source.actual_masks = ()
        source.actual_metadata = ()
        source.pa_state = 0
        if not source.pa_sync:
            if self.receiver.session is not None and self.receiver.session.identity == source.session:
                await self.receiver.stop()
            source.session = 0
            return
        await self.deactivate_other_sources(source)
        # start() publishes before returning, so use the known next receiver epoch
        # while its callback runs. No stale callback can update a replacement slot.
        source.session = self.receiver.serial + 1
        source.session = await self.receiver.start(
            source.address,
            source.sid,
            source.broadcast,
            masks=tuple(group.mask for group in source.subgroups),
            code=source.code,
            assistant=source.owner,
            prefer_past=source.pa_sync == 1,
        )

    async def write(self, owner, data):
        self.secured(owner)
        try:
            operation = parse_operation(bytes(data))
        except att.ATT_Error:
            raise
        except ValueError:
            raise att.ATT_Error(att.ErrorCode.WRITE_REQUEST_REJECTED) from None
        seen = 0
        for group in operation.subgroups:
            if group.mask != 0xFFFFFFFF:
                if seen & group.mask:
                    return  # BASS 3.1.1.1: ignore the complete conflicting operation.
                seen |= group.mask
        if self.pending >= 8:
            raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR)
        self.pending += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.lock:
                self.secured(owner)
                if operation.opcode in (0, 1):
                    # Remote Scan Started/Stopped describes the assistant's scan;
                    # it must not start/stop the receiver's local scan by itself.
                    return
                if operation.opcode == 2:
                    existing = next(
                        (
                            source
                            for source in self.sources
                            if source is not None
                            and source.address == operation.address
                            and source.sid == operation.sid
                            and source.broadcast == operation.broadcast
                        ),
                        None,
                    )
                    if existing is not None:
                        return
                    try:
                        index = self.sources.index(None)
                    except ValueError:
                        raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR) from None
                    used = {source.identity for source in self.sources if source is not None}
                    while self.serial in used:
                        self.serial = (self.serial + 1) & 0xFF
                    source = Source(
                        self.serial,
                        operation.address,
                        operation.sid,
                        operation.broadcast,
                        operation.pa_sync,
                        operation.interval,
                        operation.subgroups,
                        owner,
                    )
                    self.serial = (self.serial + 1) & 0xFF
                    self.sources[index] = source
                else:
                    index, source = self.find(operation.source)
                    if operation.opcode == 3:
                        source.pa_sync, source.interval, source.subgroups = (
                            operation.pa_sync,
                            operation.interval,
                            operation.subgroups,
                        )
                        source.owner = owner
                    elif operation.opcode == 4:
                        source.code, source.bad_code, source.encryption = operation.code, b"", 0
                        if self.receiver.session is not None and self.receiver.session.identity == source.session:
                            await self.receiver.set_code(operation.code)
                        else:
                            await self.connect(source)
                        await self.notify(index)
                        return
                    elif operation.opcode == 5:
                        # BASS permits removal only after PA/BIS synchronization
                        # has been disabled. Do not tear down a playing stream by
                        # accepting an out-of-order Remove Source.
                        if source.pa_state == 2 or any(
                            mask not in (0, 0xFFFFFFFF) for mask in source.actual_masks
                        ):
                            return
                        self.sources[index] = None
                        await self.notify(index)
                        return
                await self.connect(source)
                await self.notify(index)
        finally:
            self.pending -= 1
            self.tasks.discard(task)

    async def state_changed(self, session):
        for index, source in enumerate(self.sources):
            if source is None or source.session != session.identity:
                continue
            states = {
                "syncinfo_request": 1,
                "past_unavailable": 4,
                "synchronizing": 0,
                "reconnecting": 0,
                "failed": 3,
                "stopped": 0,
            }
            source.pa_state = states.get(session.state, 2 if session.pa is not None else 0)
            if session.state == "stopped":
                source.session = 0
            encrypted = bool(session.info and session.info.encryption) and session.state != "stopped"
            source.encryption = (
                3
                if session.state == "bad_code"
                else 1
                if session.state == "code_required"
                else 2
                if encrypted
                else 0
            )
            # BASS 1.0.1 requires the all-ones marker, never the secret key.
            source.bad_code = bytes([255]) * 16 if source.encryption == 3 else b""
            if session.base is not None:
                metas = []
                budget = 512 - 15 - len(source.subgroups) * 5 - len(source.bad_code)
                for i, requested in enumerate(source.subgroups):
                    value = requested.metadata or (
                        session.base.subgroups[i].metadata if i < len(session.base.subgroups) else b""
                    )
                    # Keep complete metadata structures; never emit an oversized
                    # characteristic or a partially truncated LTV item.
                    if len(value) > budget:
                        value = b""
                    budget -= len(value)
                    metas.append(value)
                source.actual_metadata = tuple(metas)
                source.actual_masks = tuple(
                    session.bis_mask if session.selection is not None and i == session.selection.subgroup else 0
                    for i in range(len(source.subgroups))
                )
            await self.notify(index)
        result = self.previous_callback(session)
        if inspect.isawaitable(result):
            await result

    async def close(self):
        self.closed = True
        if self.receiver.on_changed == self.state_changed:
            self.receiver.on_changed = self.previous_callback
        current = asyncio.current_task()
        pending = self.tasks - {current}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for owner, callback in tuple(self.clients.values()):
            owner.remove_listener("disconnection", callback)
        self.clients.clear()
        self.sources[:] = [None] * len(self.sources)
