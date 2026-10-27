"""Broadcast receiver control lifecycle. USB/ISO/PCM stay in the native owner."""

import asyncio
import contextlib
from dataclasses import dataclass, field
import inspect
import logging
import time

from bumble import hci
from bumble.device import BigSync, BigSyncParameters, CisLink, PeriodicAdvertisingSync
from .broadcast import Base, AnnouncementError, service_data
from .discovery_limits import DiscoveryCache, bound_periodic_fragments
from .periodic_sync import create_owned_sync

log = logging.getLogger(__name__)


class NativeBroadcastSink:
    def __init__(self, client):
        self.client, self.serial = client, 0
        self.token = None
        self.latency = None
        self.level, self.render_level = 1.0, 1.0

    async def rpc(self, operation, **values):
        return await asyncio.to_thread(self.client.request, operation, **values)

    async def prepare(self, selection):
        await self.stop()
        self.serial = (self.serial + 1) & 0xFFFFFFFF or 1
        key = f"le:broadcast:{self.serial}"
        plan = (
            self.latency.plan(selection.codec, selection.presentation_us, broadcast=True) if self.latency else None
        )
        lease = (await self.rpc("mix_add", key=key, **({"buffer_us": plan.queue_us} if plan else {})))["lease"]
        self.token = dict(key=key, lease=lease, epoch=self.serial)
        codec = selection.codec
        try:
            await self.rpc("mix_gain", key=key, lease=lease, gain=self.level * self.render_level)
            await self.rpc(
                "le_rx_prepare",
                **self.token,
                rate=codec.rate,
                duration_us=codec.duration_us,
                octets=codec.octets,
                channels=2 if selection.split else codec.channels,
                blocks=codec.blocks,
                split=selection.split,
                broadcast=True,
                controller_timestamps=True,
                allocation=0 if selection.split else codec.allocation,
            )
        except BaseException:
            await self.stop()
            raise

    async def bind(self, selection, big):
        if len(big.bis_links) != len(selection.streams):
            raise OSError("Controller returned a different BIS count")
        for lane, (stream, link) in enumerate(zip(selection.streams, big.bis_links)):
            await self.rpc(
                "le_rx_bind",
                **self.token,
                handle=link.handle,
                parent=0xFFFF,
                link_epoch=getattr(link, "_bluehead_link_epoch", 0),
                lane=lane,
            )
            await asyncio.wait_for(link.setup_data_path(direction=CisLink.Direction.CONTROLLER_TO_HOST), 2)

    async def stop(self):
        token, self.token = self.token, None
        if token is not None:
            try:
                await self.rpc("le_rx_stop", **token)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    await self.rpc("mix_remove", key=token["key"], lease=token["lease"])

    async def gain(self, value):
        if type(value) not in (float, int) or not 0 <= value <= 2:
            raise ValueError("Invalid broadcast gain")
        self.level = float(value)
        if self.token is not None:
            await self.rpc(
                "mix_gain", key=self.token["key"], lease=self.token["lease"], gain=self.level * self.render_level
            )

    async def render_gain(self, value):
        if type(value) not in (float, int) or not 0 <= value <= 1:
            raise ValueError("Invalid broadcast renderer volume")
        self.render_level = float(value)
        await self.gain(self.level)

    async def snapshot(self):
        token = self.token
        return await self.rpc("le_rx_snapshot", **token) if token is not None else {}


@dataclass(eq=False)
class Session:
    identity: int
    address: object
    sid: int
    broadcast: int
    subgroup: int | None = None
    masks: tuple | None = None
    code: bytes | None = field(default=None, repr=False)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    base: Base | None = None
    info: object = None
    pa: object = None
    big: object = None
    selection: object = None
    state: str = "synchronizing"
    message: str = ""
    bis_mask: int = 0
    assistant: object = field(default=None, repr=False)
    prefer_past: bool = False


class BroadcastReceiver:
    """One selected program, bounded discovery, explicit password and epochs.

    A bad BASE cannot replace a valid program. Real PA/BIG loss retires native
    audio before bounded retries; Cancel/Stop always reclaims pending BIG syncs.
    """

    def __init__(
        self, device, sink, *, emit=lambda *a, **k: None, changed=lambda *a: None, setup_timeout=10, retries=2
    ):
        self.device, self.sink, self.emit, self.on_changed = device, sink, emit, changed
        self.setup_timeout, self.retries = setup_timeout, retries
        self.session, self.task, self.serial = None, None, 0
        self.lock, self.candidates, self.scanning = asyncio.Lock(), {}, False
        self.closed = False
        self.orphans, self.orphan_task = {}, None
        self.failure = lambda error: None
        self.cleanup_failure = None
        self.can_play = lambda: True
        self.past = None
        device.on("periodic_advertising_sync_transfer", self.transferred)
        device.on("advertisement", self.advertisement)
        self.host = getattr(device, "host", None)
        if self.host is not None and hasattr(self.host, "on"):
            self.host.on("big_sync_establishment", self.big_identity)

    def big_identity(self, status, big_handle, *args):
        if status:
            return
        big = getattr(self.device, "big_syncs", {}).get(big_handle)
        if big is not None:
            links = big.bis_links
            end = getattr(self.host, "hci_metadata", {}).get("bluehead_link_epoch", 0)
            # The sole native event owner registers this event's BIS handles
            # consecutively before forwarding the original HCI event.
            if end >= len(links):
                for index, link in enumerate(links):
                    link._bluehead_link_epoch = end - len(links) + 1 + index

    async def publish(self, session, state, message, *, bis_mask=0):
        if self.session is not session:
            return
        session.state, session.message, session.bis_mask = state, message, bis_mask
        result = self.on_changed(session)
        if inspect.isawaitable(result):
            await result
        self.emit("broadcast", **self.snapshot())

    def snapshot(self):
        session = self.session
        programs = []
        if session is not None and session.base is not None:
            for index, group in enumerate(session.base.subgroups):
                name, language, offset = "", "", 0
                data = group.metadata
                while offset < len(data):
                    size, kind = data[offset], data[offset + 1]
                    value = data[offset + 2 : offset + 1 + size]
                    if kind == 3:
                        name = "".join(c for c in value.decode("utf-8", "replace") if c.isprintable())[:64]
                    elif kind == 4:
                        language = (
                            value.decode("ascii", "replace")
                            if len(value) == 3 and all(65 <= c <= 90 or 97 <= c <= 122 for c in value)
                            else ""
                        )
                    offset += 1 + size
                title = name or f"音频节目 {index + 1}"
                if language:
                    title += f" · {language}"
                programs.append(dict(index=index, title=title))
        return dict(
            gain=self.sink.level,
            programs=programs,
            scanning=self.scanning,
            sources=[dict(value) for value in tuple(self.candidates.values())],
            active=None
            if session is None
            else dict(
                identity=session.identity,
                address=str(session.address),
                sid=session.sid,
                broadcast_id=session.broadcast,
                state=session.state,
                message=session.message,
                subgroup=session.selection.subgroup if session.selection else session.subgroup,
                bis_mask=session.bis_mask,
                encrypted=bool(session.info and session.info.encryption),
            ),
        )

    def advertisement(self, report):
        if not self.scanning or getattr(report, "is_truncated", True) or getattr(report, "is_legacy", True):
            return
        try:
            data = service_data(report.data_bytes, 0x1852)
            if data is None or len(data) != 3 or not 0 <= report.sid <= 15:
                return
            identity = (str(report.address), report.sid, int.from_bytes(data, "little"))
            now = time.monotonic()
            for key, value in tuple(self.candidates.items()):
                if now - value["seen"] > 30:
                    self.candidates.pop(key)
            if identity not in self.candidates and len(self.candidates) >= 64:
                self.candidates.pop(min(self.candidates, key=lambda key: self.candidates[key]["seen"]))
            # Preserve names only after complete AD parsing. No remote text is executed.
            name = self.advertising_name(report.data_bytes)
            self.candidates[identity] = dict(
                address=str(report.address),
                address_type=int(report.address.address_type),
                sid=report.sid,
                broadcast_id=identity[2],
                name=name or "音频广播",
                rssi=report.rssi,
                seen=now,
            )
        except (ValueError, AttributeError):
            return

    @staticmethod
    def advertising_name(data):
        offset, name = 0, ""
        while offset < len(data) and (size := data[offset]):
            kind, value = data[offset + 1], data[offset + 2 : offset + 1 + size]
            if kind in (8, 9, 0x30):
                name = "".join(c for c in value.decode("utf-8", "replace") if c.isprintable())[:64]
            offset += size + 1
        return "".join(char for char in name if char.isprintable())

    async def scan(self, enabled):
        async with self.lock:
            if self.closed:
                raise OSError("Broadcast receiver is closed")
            if enabled == self.scanning:
                return
            if enabled:
                await asyncio.wait_for(
                    self.device.start_scanning(
                        legacy=False,
                        active=False,
                        filter_duplicates=False,
                        scan_interval=160,
                        scan_window=48,
                        scanning_phys=(hci.HCI_LE_1M_PHY,),
                    ),
                    3,
                )
            else:
                await asyncio.wait_for(self.device.stop_scanning(), 3)
            if enabled:
                self.device.advertisement_accumulators = DiscoveryCache(
                    getattr(self.device, "advertisement_accumulators", {})
                )
            self.scanning = bool(enabled)
            self.emit("broadcast", **self.snapshot())

    async def start(
        self, address, sid, broadcast, *, subgroup=None, masks=None, code=None, assistant=None, prefer_past=False
    ):
        if (
            type(sid) is not int
            or not 0 <= sid <= 15
            or type(broadcast) is not int
            or not 0 <= broadcast <= 0xFFFFFF
        ):
            raise ValueError("Invalid broadcast identity")
        if code is not None and (not isinstance(code, bytes) or len(code) != 16):
            raise ValueError("Broadcast code must contain 16 bytes")
        if masks is not None and (
            not isinstance(masks, tuple)
            or len(masks) > 31
            or any(
                type(mask) is not int or not 0 <= mask <= 0xFFFFFFFF or (mask & 0x80000000 and mask != 0xFFFFFFFF)
                for mask in masks
            )
        ):
            raise ValueError("Invalid broadcast selection")
        async with self.lock:
            if self.closed:
                raise OSError("Broadcast receiver is closed")
            if self.cleanup_failure is not None:
                raise OSError("广播后台未能安全释放资源，请先停止接收，再重新开始接收。")
            if not self.can_play():
                raise OSError("设备正在使用电脑麦克风。结束后再连接音频广播。")
            await self._stop()
            self.serial += 1
            session = self.session = Session(self.serial, address, sid, broadcast, subgroup, masks, code)
            session.assistant, session.prefer_past = assistant, bool(prefer_past)
            self.task = asyncio.create_task(self.run(session))
            await self.publish(session, "synchronizing", "正在连接音频广播…")
            return session.identity

    async def set_code(self, code):
        if not isinstance(code, bytes) or len(code) != 16:
            raise ValueError("Broadcast code must contain 16 bytes")
        session = self.session
        if session is None:
            raise OSError("No selected broadcast")
        session.code = code
        if session.state in ("bad_code", "code_required"):
            await self.publish(session, "connecting", "正在验证广播密码…")
        session.changed.set()

    async def set_program(self, index):
        session = self.session
        if session is None or session.base is None:
            raise OSError("请先连接一个音频广播")
        session.base.select(index)
        if self.task is None or self.task.done():
            await self.start(
                session.address,
                session.sid,
                session.broadcast,
                subgroup=index,
                code=session.code,
                assistant=session.assistant,
                prefer_past=session.prefer_past,
            )
        else:
            session.subgroup, session.masks = index, None
            session.changed.set()

    async def stop(self):
        async with self.lock:
            await self._stop()

    async def _stop(self):
        task = self.task
        if task is not None:
            if not task.cancelling():
                task.cancel()
            try:
                # Never cancel the task a second time while its finally block
                # is returning PA/BIG resources to the controller.
                await asyncio.wait_for(asyncio.shield(task), 8)
            except asyncio.CancelledError:
                if not task.done():
                    raise
            except TimeoutError as error:
                self.failure(OSError("Broadcast cleanup did not complete"))
                raise OSError("广播后台未能及时停止，请停止接收后重试。") from error
            if task.done():
                self.task = None
        if self.session is not None:
            await self.publish(self.session, "stopped", "音频广播已停止")
        self.session = None
        self.emit("broadcast", **self.snapshot())

    def periodic(self, session, report):
        if self.session is not session or getattr(report, "is_truncated", True):
            return
        try:
            data = service_data(report.data_bytes, 0x1851)
            if data is None:
                return
            value = Base.parse(data)
            if value != session.base:
                session.base = value
                session.changed.set()
        except ValueError:
            # Invalid radio metadata never changes the current decoder/layout.
            log.debug("Ignoring invalid BASE advertisement")

    def biginfo(self, session, info):
        if self.session is session and info != session.info:
            session.info = info
            session.changed.set()

    @staticmethod
    def listen(listeners, source, event, callback):
        source.on(event, callback)
        listeners.append((source, event, callback))

    @staticmethod
    def unlisten(listeners):
        for source, event, callback in listeners:
            with contextlib.suppress(ValueError, KeyError):
                source.remove_listener(event, callback)
        listeners.clear()

    async def wait_change(self, session, timeout=None):
        if session.lost.is_set():
            raise ConnectionError("Broadcast synchronization lost")
        changed = asyncio.create_task(session.changed.wait())
        lost = asyncio.create_task(session.lost.wait())
        try:
            done, _ = await asyncio.wait((changed, lost), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Broadcast information timed out")
            if session.lost.is_set():
                raise ConnectionError("Broadcast synchronization lost")
        finally:
            changed.cancel()
            lost.cancel()
            await asyncio.gather(changed, lost, return_exceptions=True)

    async def release_resource(self, operation):
        """Retirement uncertainty is fatal to this controller session, not loss.

        Retrying a new BIG after a lost termination reply could reuse a still
        live handle. The existing owner shuts down quietly; the UI never asks
        for administrator authorization from an unsolicited background retry.
        """
        try:
            await asyncio.wait_for(operation, 2)
            return True
        except hci.HCI_Error as error:
            # These replies positively confirm that the resource is gone.
            if error.error_code in (0x02, 0x42):
                return True
        except Exception as error:
            log.warning("Broadcast resource cleanup failed (%s)", type(error).__name__)
        if self.cleanup_failure is None:
            self.cleanup_failure = OSError("广播后台未能安全释放资源，请先停止接收，再重新开始接收。")
            self.failure(self.cleanup_failure)
            if self.session is not None:
                await self.publish(self.session, "failed", str(self.cleanup_failure))
        return False

    async def retire_big(self, session):
        # Close the native gate even if Python/controller teardown later fails.
        await self.sink.stop()
        big, session.big = session.big, None
        if big is not None:
            if not await self.release_resource(big.terminate()):
                return False
            if getattr(self.device, "big_syncs", {}).get(big.big_handle) is big:
                self.device._remove_big_sync(big)
                big.state = BigSync.State.TERMINATED
        # Cancelled create_big_sync may leave a pending entry. Remove it only
        # after a positive controller acknowledgement, never on a timeout.
        for handle, pending in tuple(getattr(self.device, "big_syncs", {}).items()):
            if pending.pa_sync is session.pa:
                if not await self.release_resource(
                    self.device.send_sync_command(hci.HCI_LE_BIG_Terminate_Sync_Command(big_handle=handle))
                ):
                    return False
                if self.device.big_syncs.get(handle) is pending:
                    self.device._remove_big_sync(pending)
                    pending.state = BigSync.State.TERMINATED
        return True

    def transferred(self, pa, owner):
        pending = self.past
        session = self.session
        if (
            pending is not None
            and not pending.done()
            and session is not None
            and session.assistant is owner
            and getattr(owner, "encryption", False)
            and pa.advertiser_address == session.address
            and pa.sid == session.sid
            and pa.state == PeriodicAdvertisingSync.State.ESTABLISHED
        ):
            session.pa = pa
            pending.set_result(pa)
        elif session is None or session.pa is not pa:
            # A transfer can race with Cancel or come from a different peer.
            # It has already allocated a controller resource, even if rejected.
            if len(self.orphans) >= 8:
                self.failure(OSError("Too many unsolicited periodic syncs"))
                return
            self.orphans[id(pa)] = pa
            if self.orphan_task is None or self.orphan_task.done():
                self.orphan_task = asyncio.create_task(self.retire_orphans())

    async def retire_orphans(self):
        while self.orphans:
            identity, pa = next(iter(self.orphans.items()))
            try:
                await asyncio.wait_for(pa.terminate(), 2)
            except Exception as error:
                self.failure(OSError("Unclaimed periodic sync cleanup failed"))
                log.warning("Periodic sync cleanup failed (%s)", type(error).__name__)
            finally:
                self.orphans.pop(identity, None)

    async def acquire_pa(self, session):
        owner = session.assistant
        supported = bool(
            getattr(getattr(self.device, "host", None), "le_features", 0)
            & hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        )
        if session.prefer_past and owner is not None and supported:
            future = self.past = asyncio.get_running_loop().create_future()
            try:
                await asyncio.wait_for(
                    self.device.send_sync_command(
                        hci.HCI_LE_Set_Periodic_Advertising_Sync_Transfer_Parameters_Command(
                            connection_handle=owner.handle, mode=2, skip=0, sync_timeout=300, cte_type=0
                        )
                    ),
                    2,
                )
                await self.publish(session, "syncinfo_request", "正在从已连接设备获取广播连接信息…")
                return await asyncio.wait_for(asyncio.shield(future), min(5, self.setup_timeout))
            except (OSError, TimeoutError, hci.HCI_Error):
                await self.publish(session, "past_unavailable", "未收到广播连接信息，正在直接搜索这个广播…")
            finally:
                self.past = None
                if not future.done():
                    future.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.device.send_sync_command(
                            hci.HCI_LE_Set_Periodic_Advertising_Sync_Transfer_Parameters_Command(
                                connection_handle=owner.handle, mode=0, skip=0, sync_timeout=300, cte_type=0
                            )
                        ),
                        2,
                    )
        return await asyncio.wait_for(
            create_owned_sync(self.device, session.address, session.sid, lambda pa: setattr(session, "pa", pa)),
            3,
        )

    async def run(self, session):
        attempt = 0
        while attempt <= self.retries and self.cleanup_failure is None:
            retry_delay = True
            listeners = []
            try:
                session.base, session.info = None, None
                session.changed.clear()
                session.lost.clear()
                session.pa = await self.acquire_pa(session)
                pa = session.pa
                bound_periodic_fragments(pa)
                self.listen(listeners, pa, "periodic_advertisement", lambda report: self.periodic(session, report))
                self.listen(listeners, pa, "biginfo_advertisement", lambda info: self.biginfo(session, info))
                self.listen(listeners, pa, "loss", session.lost.set)
                self.listen(listeners, pa, "establishment_error", session.lost.set)
                self.listen(listeners, pa, "establishment", session.changed.set)
                deadline = time.monotonic() + self.setup_timeout
                while (
                    pa.state != PeriodicAdvertisingSync.State.ESTABLISHED
                    or session.base is None
                    or session.info is None
                ):
                    session.changed.clear()
                    await self.wait_change(session, max(0.001, deadline - time.monotonic()))
                while True:
                    session.changed.clear()
                    if session.masks is not None and not any(session.masks):
                        await self.publish(session, "synchronized", "已连接广播，尚未选择播放的音频节目。")
                        await self.wait_change(session)
                        continue
                    selection = session.base.select(session.subgroup, session.masks)
                    info = session.info
                    if (
                        info.sdu_interval != selection.codec.interval_us
                        or info.max_sdu < max(stream.codec.sdu_bytes for stream in selection.streams)
                        or max(selection.indices) > info.num_bis
                    ):
                        raise AnnouncementError("BIG information does not match LC3 program")
                    if info.encryption and session.code is None:
                        await self.publish(session, "code_required", "这个音频广播需要密码，请输入广播密码。")
                        await self.wait_change(session)
                        continue
                    session.selection = selection
                    await self.publish(session, "connecting", "正在准备广播声音…")
                    await self.sink.prepare(selection)
                    session.big = await asyncio.wait_for(
                        self.device.create_big_sync(
                            pa,
                            BigSyncParameters(
                                big_sync_timeout=300,
                                bis=selection.indices,
                                broadcast_code=session.code if info.encryption else None,
                            ),
                        ),
                        8,
                    )
                    self.listen(listeners, session.big, "termination", lambda reason: session.lost.set())
                    await self.sink.bind(selection, session.big)
                    await self.publish(session, "playing", "正在播放音频广播", bis_mask=selection.mask)
                    await self.wait_change(session)
                    if not await self.retire_big(session):
                        return
            except asyncio.CancelledError:
                raise
            except hci.HCI_Error as error:
                if session.info and session.info.encryption and error.error_code in (0x05, 0x3D):
                    session.code = None
                    if not await self.retire_big(session):
                        return
                    retry_delay = False
                    session.changed.clear()
                    await self.publish(session, "bad_code", "广播密码不正确，请重新输入。")
                    # Leave the PA active while waiting for a replacement code.
                    try:
                        await self.wait_change(session)
                    except ConnectionError:
                        pass
                elif attempt == self.retries:
                    await self.publish(session, "failed", "无法连接这个音频广播，请重新搜索并连接。")
                    return
            except (OSError, ValueError) as error:
                if attempt == self.retries or isinstance(error, AnnouncementError):
                    await self.publish(session, "failed", "无法播放这个广播，请选择其它广播或音频节目。")
                    log.warning("Broadcast setup failed (%s)", type(error).__name__)
                    return
            except Exception as error:
                await self.publish(session, "failed", "广播后台遇到问题，请停止播放后重新连接。")
                log.warning("Broadcast operation failed (%s)", type(error).__name__)
                return
            finally:
                self.unlisten(listeners)
                await self.retire_big(session)
                pa, session.pa = session.pa, None
                if pa is not None:
                    await self.release_resource(pa.terminate())
            if retry_delay and self.cleanup_failure is None:
                attempt += 1
                if attempt <= self.retries:
                    await self.publish(session, "reconnecting", "广播连接中断，正在重新连接…")
                    await asyncio.sleep(0.25 * attempt)

    async def close(self):
        if self.scanning:
            with contextlib.suppress(Exception):
                await self.scan(False)
        async with self.lock:
            self.closed = True
            await self._stop()
        self.device.remove_listener("periodic_advertising_sync_transfer", self.transferred)
        self.device.remove_listener("advertisement", self.advertisement)
        if self.host is not None and hasattr(self.host, "remove_listener"):
            self.host.remove_listener("big_sync_establishment", self.big_identity)
        if self.orphan_task is not None:
            await self.orphan_task
            self.orphan_task = None
        self.candidates.clear()
