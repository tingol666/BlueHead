"""Bounded, per-ACL AVRCP controller and rendering-volume target.

Bumble supplies the SDP records, AV/C frames and AVRCP wire models.  The
application owns transactions and lifetime because those upstream helpers do
not currently bound requests, multiplex peers, or drive continuation PDUs.
All methods run on the Bluetooth asyncio loop; no PCM or audio callback runs here.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
import math
import random
import struct
import time

from bumble import avc, avctp, avrcp, core, l2cap

logger = logging.getLogger(__name__)
MAX_PDU = 65535
MAX_FRAGMENTS = 256
MAX_TEXT = 1024
CT_RECORD = 0x10020
TG_RECORD = 0x10021
Response = avc.ResponseFrame.ResponseCode
Command = avc.CommandFrame.CommandType
Pdu = avrcp.PduId
Event = avrcp.EventId
Key = avc.PassThroughFrame.OperationId


class AvrcpError(Exception):
    """Control failed; the A2DP/HFP connection remains usable."""


class PeerDisconnected(AvrcpError):
    pass


class Rejected(AvrcpError):
    pass


def _gain(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("volume must be finite")
    return min(1.0, max(0.0, value))


def _vendor(ctype, pdu, parameters, packet_type=0):
    frame_type = avc.VendorDependentCommandFrame if int(ctype) < 8 else avc.VendorDependentResponseFrame
    return frame_type(ctype, avc.Frame.SubunitType.PANEL, 0,
                      avrcp.AVRCP_BLUETOOTH_SIG_COMPANY_ID,
                      struct.pack(">BBH", int(pdu), packet_type, len(parameters)) + parameters)


def _parameters(frame):
    data = frame.vendor_dependent_data
    if frame.company_id != avrcp.AVRCP_BLUETOOTH_SIG_COMPANY_ID or len(data) < 4:
        raise ValueError("invalid vendor header")
    pdu, flags, length = struct.unpack_from(">BBH", data)
    if flags & 0xFC or length != len(data) - 4:
        raise ValueError("invalid PDU length or reserved bits")
    return pdu, flags & 3, data[4:]


class _Transport:
    """AVCTP control framing, including bounded interleaved reassembly.

    Continuation/end AVCTP packets have a one-byte header, unlike start and
    single packets.  AVRCP continuation is a separate layer handled by Peer.
    """
    def __init__(self, channel, on_message, timeout):
        self.channel, self.on_message, self.timeout = channel, on_message, timeout
        self.fragments = {}
        channel.sink = self.receive

    def send(self, label, is_command, payload, *, pid=avrcp.AVRCP_PID, ipid=False):
        mtu = max(48, int(self.channel.peer_mtu))
        base = label << 4 | (0 if is_command else 2) | int(ipid)
        if len(payload) + 3 <= mtu:
            self.channel.write(bytes([base]) + struct.pack(">H", pid) + payload)
            return
        first, rest = payload[:mtu - 4], payload[mtu - 4:]
        count = 1 + (len(rest) + mtu - 2) // (mtu - 1)
        if count > 255 or len(payload) > MAX_PDU + 10:
            raise ValueError("AVCTP message exceeds fragment limit")
        self.channel.write(bytes([base | 4, count]) + struct.pack(">H", pid) + first)
        while rest:
            chunk, rest = rest[:mtu - 1], rest[mtu - 1:]
            self.channel.write(bytes([base | (8 if rest else 12)]) + chunk)

    def receive(self, data):
        try:
            self._receive(bytes(data))
        except (ValueError, IndexError, struct.error, core.InvalidPacketError):
            # Metadata and raw packets deliberately never enter diagnostics.
            logger.debug("ignored malformed AVCTP packet")

    def _receive(self, data):
        if not data:
            return
        now = time.monotonic()
        self.fragments = {k: v for k, v in self.fragments.items() if now - v[4] < self.timeout}
        head = data[0]
        label, kind, is_command, ipid = head >> 4, (head >> 2) & 3, not bool(head & 2), bool(head & 1)
        key = label, is_command
        if is_command and ipid:
            raise ValueError("invalid IPID command")
        if kind in (0, 1):
            offset = 3 if kind == 0 else 4
            if len(data) < offset:
                raise ValueError("short AVCTP header")
            pid = int.from_bytes(data[offset - 2:offset], "big")
            self.fragments.pop(key, None)
            if kind == 0:
                self.on_message(label, is_command, ipid, pid, data[offset:])
                return
            count = data[1]
            if count < 2 or count > 255:
                raise ValueError("invalid fragment count")
            self.fragments[key] = [pid, ipid, count, bytearray(data[offset:]), now, 1, len(data)]
            return
        state = self.fragments.get(key)
        if state is None:
            return
        state[3].extend(data[1:])
        state[5] += 1
        if ipid or len(state[3]) > MAX_PDU + 10 or state[5] > state[2] or len(data) > state[6] or kind == 2 and len(data) != state[6]:
            self.fragments.pop(key, None)
            raise ValueError("invalid AVCTP fragment")
        if kind == 3:
            self.fragments.pop(key, None)
            if state[5] != state[2]:
                raise ValueError("early final fragment")
            self.on_message(label, is_command, state[1], state[0], bytes(state[3]))
        elif state[5] == state[2]:
            self.fragments.pop(key, None)
            raise ValueError("missing final fragment")


@dataclass(eq=False)
class _Transaction:
    pdu: int | None
    future: asyncio.Future
    changed: asyncio.Future | None = None
    event: int | None = None
    labels: set = field(default_factory=set)
    data: bytearray = field(default_factory=bytearray)
    fragment_count: int = 0
    response_code: int | None = None
    key_event: tuple | None = None


class Peer:
    def __init__(self, manager, connection):
        self.manager, self.connection = manager, connection
        self.key = str(connection.peer_address)
        self.channel = self.transport = None
        self.closed = False
        self.connecting = False
        self.retry_task = None
        self.tasks = set()
        self.pending = {}
        self.labels = deque(range(16))
        self.quarantined = {}
        try:
            self.volume = round(_gain(manager.get_volume(self.key)) * 127)
        except Exception:
            self.volume = 127
            logger.warning("AVRCP initial volume callback failed")
        self.volume_listener = None
        self.state = dict(connected=False, title="", artist="", album="", duration_ms=None,
                          position_ms=None, playback_status="unknown", controls=False,
                          volume=self.volume / 127)
        self.metadata_task = None
        self.metadata_dirty = False
        self.metadata_revision = 0
        self.supported_events = set()
        self.command_lock = asyncio.Lock()
        connection.once(connection.EVENT_DISCONNECTION, lambda *_: self.close())

    def spawn(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        def done(completed):
            self.tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.debug("AVRCP control operation failed (%s)", type(completed.exception()).__name__)
        task.add_done_callback(done)
        return task

    def publish(self, **updates):
        if self.closed:
            return
        self.state.update(updates)
        self.manager.emit("avrcp", key=self.key, **self.state)

    def bind(self, channel):
        if self.closed:
            self.spawn(channel.disconnect())
            return
        if self.channel is channel:
            return
        if self.channel is not None:
            # AVRCP 4.1.1: close both colliding control channels then retry.
            previous = self.channel
            self.unbind(previous)
            self.spawn(self._disconnect_channel(previous))
            self.spawn(self._disconnect_channel(channel))
            async def retry_collision():
                await asyncio.sleep(random.uniform(.1, 1.0))
                self.schedule_connect(0)
            self.spawn(retry_collision())
            return
        self.channel = channel
        self.transport = _Transport(channel, lambda *args: self.receive(channel, *args), self.manager.command_timeout)
        channel.on(channel.EVENT_CLOSE, lambda: self.channel_closed(channel))
        self.publish(connected=True, controls=True)
        self.spawn(self.bootstrap())

    @staticmethod
    async def _disconnect_channel(channel):
        try:
            async with asyncio.timeout(2.0):
                await channel.disconnect()
        except Exception:
            pass

    def unbind(self, channel):
        if self.channel is not channel:
            return
        self.channel = self.transport = None
        self.volume_listener = None
        self._cancel_pending()
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current and task is not self.retry_task:
                task.cancel()
        self.metadata_task = None
        self.publish(connected=False, controls=False, title="", artist="", album="", duration_ms=None,
                     position_ms=None, playback_status="unknown")

    def channel_closed(self, channel):
        if self.channel is not channel:
            return
        self.unbind(channel)
        if self.manager.auto_connect and not self.manager.closed and not self.closed:
            self.schedule_connect()

    def _cancel_pending(self):
        for transaction in set(self.pending.values()):
            for future in (transaction.future, transaction.changed):
                if future is not None and not future.done():
                    future.cancel()
        self.pending.clear()
        self.labels = deque(range(16))
        self.quarantined.clear()

    def close(self):
        if self.closed:
            return
        channel = self.channel
        if channel is not None:
            self.unbind(channel)
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        self._cancel_pending()
        if self.manager.peers.get(self.key) is self:
            del self.manager.peers[self.key]

    def schedule_connect(self, delay=.35):
        if self.closed or self.retry_task and not self.retry_task.done():
            return
        async def retry():
            await asyncio.sleep(delay)
            for attempt in range(3):
                if self.closed or self.channel:
                    return
                try:
                    await self.connect()
                    return
                except Exception:
                    await asyncio.sleep(.3 * (attempt + 1) + random.random() * .2)
        self.retry_task = self.spawn(retry())

    async def connect(self):
        if self.closed:
            raise PeerDisconnected()
        if self.channel is not None or self.connecting:
            return
        self.connecting = True
        try:
            async with asyncio.timeout(self.manager.command_timeout):
                channel = await self.connection.create_l2cap_channel(l2cap.ClassicChannelSpec(psm=avctp.AVCTP_PSM))
            self.bind(channel)
        finally:
            self.connecting = False

    def _label(self, transaction):
        if not self.labels:
            # A four-bit label has no sequence number. Never reuse an abandoned
            # label on the same channel: a late reply could complete a new request.
            # Recover the control channel if repeated peer timeouts consume it.
            if self.quarantined and self.channel:
                channel = self.channel
                self.unbind(channel)
                self.spawn(self._disconnect_channel(channel))
                self.schedule_connect()
            raise AvrcpError("all control transactions are occupied")
        label = self.labels.popleft()
        self.pending[label] = transaction
        transaction.labels.add(label)
        return label

    def _release_label(self, transaction, label, quarantine=False):
        if self.pending.get(label) is transaction:
            del self.pending[label]
            transaction.labels.discard(label)
            if quarantine:
                self.quarantined[label] = True
            else:
                self.labels.append(label)

    def _retire(self, transaction, quarantine=False):
        for label in list(transaction.labels):
            self._release_label(transaction, label, quarantine)

    def _send(self, label, frame):
        if self.transport is None or self.closed:
            raise PeerDisconnected()
        self.transport.send(label, True, bytes(frame))

    async def request(self, pdu, parameters=b"", ctype=Command.STATUS):
        loop = asyncio.get_running_loop()
        transaction = _Transaction(int(pdu), loop.create_future())
        label = self._label(transaction)
        timed_out = False
        try:
            self._send(label, _vendor(ctype, pdu, parameters))
            async with asyncio.timeout(self.manager.command_timeout):
                code, result = await transaction.future
            if code not in (Response.IMPLEMENTED_OR_STABLE, Response.ACCEPTED):
                raise Rejected(f"control PDU {int(pdu):02x} rejected")
            return result
        except (TimeoutError, asyncio.CancelledError):
            timed_out = True
            if transaction.fragment_count and self.transport:
                self._abort_continuation(pdu)
            raise
        except AvrcpError as error:
            timed_out = not isinstance(error, Rejected)
            raise
        finally:
            self._retire(transaction, timed_out)

    def _abort_continuation(self, pdu):
        # Abort has its own transaction and must not displace an existing one.
        async def abort():
            try:
                await self.request(Pdu.ABORT_CONTINUING_RESPONSE, bytes([pdu]), Command.CONTROL)
            except (AvrcpError, TimeoutError):
                pass
        self.spawn(abort())

    async def press(self, key):
        async with self.command_lock:
            try:
                await self._key(key, True)
            finally:
                # Even a lost press response may mean the target is holding a key.
                if self.transport and not self.closed:
                    release = self.spawn(self._key(key, False))
                    await asyncio.shield(release)

    async def _key(self, key, pressed):
        flag = avc.PassThroughFrame.StateFlag.PRESSED if pressed else avc.PassThroughFrame.StateFlag.RELEASED
        transaction = _Transaction(None, asyncio.get_running_loop().create_future(), key_event=(key, flag))
        label = self._label(transaction)
        uncertain = False
        try:
            self._send(label, avc.PassThroughCommandFrame(Command.CONTROL, avc.Frame.SubunitType.PANEL, 0,
                                                        flag, key, b""))
            async with asyncio.timeout(self.manager.command_timeout):
                code, _ = await transaction.future
            if code != Response.ACCEPTED:
                raise Rejected("media key rejected")
        except (TimeoutError, asyncio.CancelledError):
            uncertain = True
            raise
        finally:
            self._retire(transaction, uncertain)

    def receive(self, channel, label, is_command, ipid, pid, payload):
        if self.closed or self.channel is not channel:
            return
        try:
            if pid != avrcp.AVRCP_PID:
                if is_command:
                    self.transport.send(label, False, b"", pid=pid, ipid=True)
                return
            if ipid:
                if transaction := self.pending.get(label):
                    if not transaction.future.done():
                        transaction.future.set_exception(Rejected("peer does not support AVRCP"))
                return
            frame = avc.Frame.from_bytes(payload)
            if is_command and isinstance(frame, avc.CommandFrame):
                self._command(label, frame)
            elif not is_command and isinstance(frame, avc.ResponseFrame):
                self._response(label, frame)
        except (ValueError, IndexError, struct.error, core.InvalidPacketError, NotImplementedError):
            logger.debug("ignored malformed AVRCP message")
            transaction = self.pending.get(label)
            if not is_command and transaction:
                error = AvrcpError("malformed control response")
                if not transaction.future.done():
                    transaction.future.set_exception(error)
                elif transaction.changed is not None and not transaction.changed.done():
                    transaction.changed.set_exception(error)

    def _respond(self, label, frame):
        if self.transport:
            self.transport.send(label, False, bytes(frame))

    def _reject(self, label, frame, code=Response.NOT_IMPLEMENTED):
        self._respond(label, avc.ResponseFrame(code, frame.subunit_type, frame.subunit_id, frame.opcode, frame.operands))

    def _command(self, label, frame):
        if frame.opcode in (avc.Frame.OperationCode.UNIT_INFO, avc.Frame.OperationCode.SUBUNIT_INFO):
            if frame.ctype != Command.STATUS or frame.subunit_type != avc.Frame.SubunitType.UNIT or frame.subunit_id != 7:
                self._reject(label, frame, Response.REJECTED)
                return
            if len(frame.operands) != 5:
                self._reject(label, frame, Response.REJECTED)
                return
            if frame.opcode == avc.Frame.OperationCode.UNIT_INFO:
                operands = bytes([7, avc.Frame.SubunitType.PANEL << 3, 0xFF, 0xFF, 0xFF])
            else:
                page = frame.operands[0]
                operands = bytes([page, avc.Frame.SubunitType.PANEL << 3, 0xFF, 0xFF, 0xFF]) if page == 7 else bytes([page, 255, 255, 255, 255])
            self._respond(label, avc.ResponseFrame(Response.IMPLEMENTED_OR_STABLE, frame.subunit_type,
                          frame.subunit_id, frame.opcode, operands))
            return
        if frame.subunit_type != avc.Frame.SubunitType.PANEL or frame.subunit_id != 0:
            self._reject(label, frame)
            return
        if isinstance(frame, avc.PassThroughCommandFrame):
            if frame.ctype != Command.CONTROL or frame.operation_id not in (Key.VOLUME_UP, Key.VOLUME_DOWN, Key.MUTE):
                self._reject(label, frame)
                return
            if frame.state_flag == avc.PassThroughFrame.StateFlag.PRESSED:
                if frame.operation_id == Key.MUTE:
                    value = 0 if self.volume else 127
                else:
                    value = self.volume + (8 if frame.operation_id == Key.VOLUME_UP else -8)
                self.set_volume(value, notify=True, remote=True)
            self._respond(label, avc.PassThroughResponseFrame(Response.ACCEPTED, frame.subunit_type,
                          frame.subunit_id, frame.state_flag, frame.operation_id, frame.operation_data))
            return
        if not isinstance(frame, avc.VendorDependentCommandFrame):
            self._reject(label, frame)
            return
        if frame.company_id != avrcp.AVRCP_BLUETOOTH_SIG_COMPANY_ID:
            self._reject(label, frame)
            return
        pdu, kind, parameters = _parameters(frame)
        if kind:
            self._respond(label, _vendor(Response.REJECTED, pdu, bytes([avrcp.StatusCode.INVALID_PARAMETER])))
            return
        if pdu == Pdu.GET_CAPABILITIES and frame.ctype == Command.STATUS and len(parameters) == 1:
            if parameters == b"\x03":
                response = b"\x03\x01" + bytes([Event.VOLUME_CHANGED])
            elif parameters == b"\x02":
                response = b"\x02\x01" + avrcp.AVRCP_BLUETOOTH_SIG_COMPANY_ID.to_bytes(3, "big")
            else:
                self._respond(label, _vendor(Response.REJECTED, pdu, bytes([avrcp.StatusCode.INVALID_PARAMETER])))
                return
            self._respond(label, _vendor(Response.IMPLEMENTED_OR_STABLE, pdu, response))
        elif pdu == Pdu.SET_ABSOLUTE_VOLUME and frame.ctype == Command.CONTROL and len(parameters) == 1 and parameters[0] < 128:
            self.set_volume(parameters[0], notify=False, remote=True)
            self._respond(label, _vendor(Response.ACCEPTED, pdu, bytes([self.volume])))
        elif pdu == Pdu.REGISTER_NOTIFICATION and frame.ctype == Command.NOTIFY and len(parameters) == 5 and parameters[0] == Event.VOLUME_CHANGED:
            self.volume_listener = label
            self._respond(label, _vendor(Response.INTERIM, pdu, bytes([Event.VOLUME_CHANGED, self.volume])))
        else:
            self._respond(label, _vendor(Response.REJECTED, pdu, bytes([avrcp.StatusCode.INVALID_PARAMETER])))

    def set_volume(self, value, *, notify, remote=False):
        value = min(127, max(0, int(value)))
        changed = value != self.volume
        self.volume = value
        if remote:
            self.manager.on_volume(self.key, value / 127)
        if changed:
            self.publish(volume=value / 127)
        if changed and notify and self.volume_listener is not None:
            label, self.volume_listener = self.volume_listener, None
            self._respond(label, _vendor(Response.CHANGED, Pdu.REGISTER_NOTIFICATION, bytes([Event.VOLUME_CHANGED, value])))

    def _response(self, label, frame):
        transaction = self.pending.get(label)
        if transaction is None:
            return
        if isinstance(frame, avc.PassThroughResponseFrame) and transaction.pdu is None:
            if (frame.operation_id, frame.state_flag) != transaction.key_event:
                return
            if not transaction.future.done():
                transaction.future.set_result((frame.response, b""))
            return
        if not isinstance(frame, avc.VendorDependentResponseFrame):
            return
        pdu, kind, parameters = _parameters(frame)
        if pdu != transaction.pdu:
            return
        if kind:
            if transaction.changed is not None or frame.response not in (Response.IMPLEMENTED_OR_STABLE, Response.ACCEPTED):
                raise ValueError("invalid fragmented response")
            if kind == 1:
                if transaction.fragment_count:
                    raise ValueError("duplicate fragment start")
                transaction.response_code = frame.response
            elif not transaction.fragment_count or frame.response != transaction.response_code:
                raise ValueError("orphan response fragment")
            transaction.fragment_count += 1
            transaction.data.extend(parameters)
            if len(transaction.data) > MAX_PDU or transaction.fragment_count > MAX_FRAGMENTS:
                self._abort_continuation(pdu)
                if not transaction.future.done():
                    transaction.future.set_exception(AvrcpError("metadata exceeds limits"))
                return
            if kind in (1, 2):
                self._release_label(transaction, label)
                continuation_label = self._label(transaction)
                self._send(continuation_label, _vendor(Command.CONTROL, Pdu.REQUEST_CONTINUING_RESPONSE, bytes([pdu])))
                return
            parameters = bytes(transaction.data)
        elif transaction.fragment_count:
            raise ValueError("unexpected unfragmented continuation")
        if transaction.changed is not None:
            if frame.response not in (Response.INTERIM, Response.CHANGED):
                if not transaction.future.done():
                    transaction.future.set_result((frame.response, parameters))
                elif not transaction.changed.done():
                    transaction.changed.set_result((frame.response, parameters))
                return
            if not parameters or parameters[0] != transaction.event:
                raise ValueError("notification does not match subscription")
            expected = {Event.PLAYBACK_STATUS_CHANGED: 2, Event.TRACK_CHANGED: 9,
                        Event.NOW_PLAYING_CONTENT_CHANGED: 1, Event.ADDRESSED_PLAYER_CHANGED: 5,
                        Event.PLAYBACK_POS_CHANGED: 5}
            if len(parameters) != expected.get(transaction.event, len(parameters)):
                raise ValueError("invalid notification length")
            if frame.response == Response.INTERIM:
                if not transaction.future.done():
                    transaction.future.set_result((frame.response, parameters))
                return
            if not transaction.future.done():
                transaction.future.set_result((frame.response, parameters))
            if not transaction.changed.done():
                transaction.changed.set_result((frame.response, parameters))
        elif not transaction.future.done():
            transaction.future.set_result((frame.response, parameters))

    async def bootstrap(self):
        supported = set()
        try:
            parameters = await self.request(Pdu.GET_CAPABILITIES, b"\x03")
            if len(parameters) >= 2 and parameters[0] == 3 and len(parameters) == 2 + parameters[1]:
                supported = set(parameters[2:])
        except (AvrcpError, TimeoutError):
            pass
        self.supported_events = supported
        for event in (Event.PLAYBACK_STATUS_CHANGED, Event.TRACK_CHANGED, Event.NOW_PLAYING_CONTENT_CHANGED,
                      Event.ADDRESSED_PLAYER_CHANGED, Event.PLAYBACK_POS_CHANGED):
            if event in supported:
                self.spawn(self.monitor(event))
        try:
            await self.refresh_status()
        except (AvrcpError, TimeoutError, ValueError):
            pass
        self.refresh_metadata()

    async def refresh_status(self):
        data = await self.request(Pdu.GET_PLAY_STATUS)
        if len(data) != 9:
            raise ValueError("invalid play status length")
        duration, position, status = struct.unpack(">IIB", data)
        self.publish(duration_ms=None if duration == 0xFFFFFFFF else duration,
                     position_ms=None if position == 0xFFFFFFFF else position,
                     playback_status=_status(status))

    async def monitor(self, event):
        while self.channel and not self.closed:
            loop = asyncio.get_running_loop()
            transaction = _Transaction(int(Pdu.REGISTER_NOTIFICATION), loop.create_future(), loop.create_future(), int(event))
            label = self._label(transaction)
            uncertain = False
            try:
                interval = 1 if event == Event.PLAYBACK_POS_CHANGED else 0
                self._send(label, _vendor(Command.NOTIFY, Pdu.REGISTER_NOTIFICATION, struct.pack(">BI", event, interval)))
                async with asyncio.timeout(self.manager.command_timeout):
                    code, data = await transaction.future
                if code not in (Response.INTERIM, Response.CHANGED):
                    return
                self.handle_event(data)
                if code == Response.INTERIM:
                    code, data = await transaction.changed
                    if code != Response.CHANGED:
                        return
                    self.handle_event(data)
            except (AvrcpError, TimeoutError, ValueError):
                uncertain = True
                return
            finally:
                self._retire(transaction, uncertain)
                if not transaction.changed.done():
                    transaction.changed.cancel()

    def handle_event(self, data):
        if data[0] == Event.PLAYBACK_STATUS_CHANGED and len(data) == 2:
            self.publish(playback_status=_status(data[1]))
        elif data[0] == Event.PLAYBACK_POS_CHANGED and len(data) == 5:
            value = int.from_bytes(data[1:], "big")
            self.publish(position_ms=None if value == 0xFFFFFFFF else value)
        elif data[0] == Event.TRACK_CHANGED and len(data) == 9:
            if data[1:] == b"\xff" * 8:
                self.metadata_revision += 1
                self.metadata_dirty = False
                self.publish(title="", artist="", album="", duration_ms=None, position_ms=None)
            else:
                self.refresh_metadata()
        elif data[0] in (Event.NOW_PLAYING_CONTENT_CHANGED, Event.ADDRESSED_PLAYER_CHANGED):
            self.refresh_metadata()

    def refresh_metadata(self):
        self.metadata_revision += 1
        self.metadata_dirty = True
        if self.metadata_task is not None and not self.metadata_task.done():
            return
        async def refresh():
            while self.metadata_dirty and self.channel and not self.closed:
                self.metadata_dirty = False
                revision = self.metadata_revision
                try:
                    request = avrcp.GetElementAttributesCommand(0, [avrcp.MediaAttributeId.TITLE,
                        avrcp.MediaAttributeId.ARTIST_NAME, avrcp.MediaAttributeId.ALBUM_NAME, avrcp.MediaAttributeId.PLAYING_TIME])
                    data = await self.request(request.pdu_id, bytes(request))
                    metadata = _metadata(data)
                    if revision == self.metadata_revision:
                        self.publish(**metadata)
                except (AvrcpError, TimeoutError, ValueError):
                    return
        self.metadata_task = self.spawn(refresh())


def _status(status):
    return {0: "stopped", 1: "playing", 2: "paused", 3: "forward_seek", 4: "reverse_seek", 255: "error"}.get(status, "unknown")


def _metadata(data):
    if not data or len(data) > MAX_PDU:
        raise ValueError("invalid metadata size")
    offset, result = 1, dict(title="", artist="", album="", duration_ms=None)
    encodings = {3: "ascii", 4: "iso8859-1", 106: "utf-8", 1013: "utf-16-be", 1014: "utf-16-le", 1015: "utf-16"}
    for _ in range(data[0]):
        if len(data) - offset < 8:
            raise ValueError("short metadata attribute")
        identifier, charset, length = struct.unpack_from(">IHH", data, offset)
        offset += 8
        if length > len(data) - offset:
            raise ValueError("metadata attribute overrun")
        encoded = data[offset:offset + length]
        offset += length
        if charset not in encodings:
            continue
        text = encoded.decode(encodings[charset], errors="replace").replace("\x00", "")[:MAX_TEXT]
        if identifier in (1, 2, 3):
            result[{1: "title", 2: "artist", 3: "album"}[identifier]] = text
        elif identifier == 7 and text.isascii() and text.isdecimal() and len(text) <= 10:
            result["duration_ms"] = min(int(text), 0xFFFFFFFE)
    if offset != len(data):
        raise ValueError("trailing metadata bytes")
    return result


class AvrcpManager:
    """Call on the Bluetooth loop; callbacks must be fast, synchronous functions.

    get_volume/on_volume describe standard 0..1 rendering volume.  Any 100..200%
    software boost remains a separate audio gain.  emit receives ephemeral
    metadata; callers should not persist it in logs or diagnostic exports.
    """
    def __init__(self, emit=None, on_volume=None, get_volume=None, *, command_timeout=2.0, auto_connect=True):
        self.emit_callback = emit or (lambda *_args, **_kwargs: None)
        self.volume_callback = on_volume or (lambda *_: None)
        self.get_volume = get_volume or (lambda _: 1.0)
        self.command_timeout = max(.05, float(command_timeout))
        self.auto_connect = auto_connect
        self.device = self.server = None
        self.records = {}
        self.peers = {}
        self.closed = False

    def emit(self, *args, **kwargs):
        try:
            self.emit_callback(*args, **kwargs)
        except Exception:
            logger.warning("AVRCP state callback failed")

    def on_volume(self, key, gain):
        try:
            self.volume_callback(key, gain)
        except Exception:
            logger.warning("AVRCP volume callback failed")

    def attach(self, device):
        if self.device is device:
            return self
        if self.device is not None or self.closed:
            raise RuntimeError("manager is already attached or closed")
        records = device.sdp_service_records
        if CT_RECORD in records or TG_RECORD in records:
            raise RuntimeError("AVRCP SDP record handles are already occupied")
        self.server = device.create_l2cap_server(l2cap.ClassicChannelSpec(psm=avctp.AVCTP_PSM), self._incoming)
        self.device = device
        self.records = {
            CT_RECORD: avrcp.ControllerServiceSdpRecord(CT_RECORD,
                supported_features=avrcp.ControllerFeatures.CATEGORY_1).to_service_attributes(),
            TG_RECORD: avrcp.TargetServiceSdpRecord(TG_RECORD,
                supported_features=avrcp.TargetFeatures.CATEGORY_2).to_service_attributes(),
        }
        records.update(self.records)
        device.on("connection", self.on_connection)
        return self

    def on_connection(self, connection):
        if self.closed or connection.transport != core.PhysicalTransport.BR_EDR:
            return None
        key = str(connection.peer_address)
        peer = self.peers.get(key)
        if peer is not None and peer.connection is connection:
            return peer
        if peer:
            peer.close()
        peer = self.peers[key] = Peer(self, connection)
        if self.auto_connect:
            peer.schedule_connect()
        return peer

    def _incoming(self, channel):
        peer = self.on_connection(channel.connection)
        if peer is not None:
            channel.on(channel.EVENT_OPEN, lambda: peer.bind(channel))

    async def connect(self, connection):
        peer = self.on_connection(connection)
        if peer is None:
            raise PeerDisconnected()
        await peer.connect()
        return peer

    async def disconnect(self, key):
        peer = self.peers.get(key)
        if peer and peer.channel:
            channel = peer.channel
            peer.unbind(channel)
            await peer._disconnect_channel(channel)

    async def command(self, key, command):
        peer = self.peers.get(key)
        if peer is None or peer.channel is None:
            raise PeerDisconnected()
        if command == "play_pause":
            if Event.PLAYBACK_STATUS_CHANGED not in peer.supported_events or peer.state["playback_status"] not in ("playing", "paused", "stopped"):
                await peer.refresh_status()
            if peer.state["playback_status"] not in ("playing", "paused", "stopped"):
                raise AvrcpError("peer playback state is unavailable")
            command = "pause" if peer.state["playback_status"] == "playing" else "play"
        keys = {"play": Key.PLAY, "pause": Key.PAUSE, "stop": Key.STOP,
                "next": Key.FORWARD, "previous": Key.BACKWARD}
        if command not in keys:
            raise ValueError("unsupported media command")
        await peer.press(keys[command])

    def set_volume(self, key, gain, *, notify=True):
        peer = self.peers.get(key)
        if peer:
            peer.set_volume(round(_gain(gain) * 127), notify=notify)

    def snapshot(self, key):
        peer = self.peers.get(key)
        return dict(peer.state) if peer else None

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        if self.device:
            self.device.remove_listener("connection", self.on_connection)
            for handle, record in self.records.items():
                if self.device.sdp_service_records.get(handle) is record:
                    self.device.sdp_service_records.pop(handle, None)
        if self.server:
            self.server.close()
        tasks, channels = [], []
        for peer in list(self.peers.values()):
            if peer.channel:
                channels.append(peer.channel)
            peer.close()
            tasks.extend(peer.tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(Peer._disconnect_channel(channel) for channel in channels), return_exceptions=True)
