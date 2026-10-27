"""Bluetooth microphone admission, before AT+BCS or HCI acceptance.

Admission is independent of the negotiated voice codec (CVSD, mSBC or LC3-SWB).

Control only: the existing host remains the sole HCI command owner. Audio and
controller-health detection are unchanged. Never infer a call from media silence.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from bumble import hci
from bumble.core import PhysicalTransport
from bumble.device import Device


@dataclass(eq=False)
class Reservation:
    connection: object
    link: object = None
    timer: asyncio.TimerHandle | None = None


class VoiceProtection:
    def __init__(self, device, emit, enabled=True, *, timeout=2.0, pending_timeout=10.0, include_le=False):
        self.device, self.emit, self.enabled = device, emit, enabled
        self.include_le = bool(include_le)
        self.timeout, self.pending_timeout = timeout, pending_timeout
        self.reservation = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.last_attempt_notice = float('-inf')
        self.inflight = set()
        self.tasks = set()
        self.disconnects = {}

    def peers(self):
        return [peer for peer in list(self.device.connections.values())
                if peer.transport == PhysicalTransport.BR_EDR or self.include_le and peer.transport == PhysicalTransport.LE]

    def current(self, connection):
        return any(peer is connection for peer in self.peers())

    def allowed(self, connection):
        return not self.enabled or self.reservation is None or self.reservation.connection is connection

    def event(self, state, message, **extra):
        self.emit('voice_protection', state=state, message=message, **extra)

    def blocked_notice(self):
        # Repeated automatic reconnects must not flood the page or log.
        now = time.monotonic()
        if now - self.last_attempt_notice >= 3:
            self.last_attempt_notice = now
            self.event('blocked', '有其它设备尝试连接。当前设备正在使用电脑麦克风，为避免声音中断，暂时只允许连接这一台。结束后可重新连接其它设备。')

    def request_blocked(self, address):
        if self.enabled and self.reservation is not None:
            self.blocked_notice()
            return True
        return False

    def drop(self, connection):
        identity = id(connection)
        if identity in self.disconnects:
            return self.disconnects[identity]
        async def disconnect():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.disconnect(), self.timeout)
        task = asyncio.create_task(disconnect())
        self.disconnects[identity] = task
        self.tasks.add(task)
        def done(result):
            self.tasks.discard(result)
            if self.disconnects.get(identity) is result:
                self.disconnects.pop(identity, None)
            if not result.cancelled():
                result.exception()
        task.add_done_callback(done)
        return task

    def arrived(self, connection):
        self.inflight.discard(str(connection.peer_address))
        if self.allowed(connection):
            return True
        self.blocked_notice()
        self.drop(connection)
        return False

    def expired(self, reservation):
        if self.reservation is reservation and reservation.link is None:
            self.clear(reservation, 'idle', '')

    def clear(self, reservation, state='idle', message=''):
        if self.reservation is not reservation:
            return
        if reservation.timer:
            reservation.timer.cancel()
        self.reservation = None
        self.event(state, message)

    async def prepare(self, connection, codec):
        if self.closed or not self.current(connection):
            return False
        if not self.enabled:
            if self.reservation and self.reservation.connection is connection:
                self.failed(connection)
            return True
        async with self.lock:
            if self.closed or not self.current(connection):
                return False
            if not self.enabled:
                return True
            if self.reservation and self.reservation.connection is not connection:
                self.blocked_notice()
                return False
            reservation = self.reservation
            if reservation is None:
                reservation = self.reservation = Reservation(connection)
                reservation.timer = asyncio.get_running_loop().call_later(
                    self.pending_timeout, self.expired, reservation)
            others = [peer for peer in self.peers() if peer is not connection]
            if others or self.inflight:
                self.event('preparing', '设备要使用电脑麦克风，正在断开其它设备，以免声音中断。',
                           key=str(connection.peer_address))
            # Complete disconnections before acknowledging codec negotiation.
            # No silent fallback if one ACL refuses to retire.
            deadline = asyncio.get_running_loop().time() + self.timeout
            dropping = set()
            jobs = set()
            try:
                before = getattr(self, 'before_prepare', None)
                if before is not None:
                    await before(connection, codec)
                    deadline = asyncio.get_running_loop().time() + self.timeout
                while self.reservation is reservation and self.enabled and not self.closed:
                    if not self.current(connection):
                        return False
                    others = [peer for peer in self.peers() if peer is not connection]
                    for peer in others:
                        if id(peer) not in dropping:
                            dropping.add(id(peer))
                            jobs.add(self.drop(peer))
                    if not others and not self.inflight:
                        self.event('active', '通话／语音保护已开启：暂时只连接使用电脑麦克风的设备。麦克风使用结束后，可重新连接其它设备。',
                                   key=str(connection.peer_address))
                        return True
                    if asyncio.get_running_loop().time() >= deadline:
                        if reservation.link is None:
                            self.clear(reservation, 'failed', '其它设备尚未断开，暂未启用电脑麦克风。请先在其它设备的蓝牙设置中断开与电脑的连接，再重试。')
                        else:
                            self.event('failed', '其它设备尚未断开，暂未建立新的语音连接；当前麦克风保护仍保留。请先在其它设备的蓝牙设置中断开与电脑的连接，再重试。')
                        return False
                    await asyncio.sleep(.02)
                return not self.enabled and not self.closed and self.current(connection)
            except BaseException:
                if reservation.link is None:
                    self.clear(reservation)
                raise
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                if self.reservation is reservation and not self.current(connection):
                    self.clear(reservation)

    def activate(self, connection, link, codec):
        if self.closed or not self.current(connection):
            return False
        if not self.enabled:
            return True
        reservation = self.reservation
        if (reservation is None or reservation.connection is not connection or self.inflight or
                any(peer is not connection for peer in self.peers())):
            return False
        reservation.link = link
        if reservation.timer:
            reservation.timer.cancel()
            reservation.timer = None
        return True

    def failed(self, connection):
        reservation = self.reservation
        if reservation and reservation.connection is connection and reservation.link is None:
            self.clear(reservation)

    def ended(self, connection, link=None):
        reservation = self.reservation
        if reservation and reservation.connection is connection and (link is None or reservation.link is link):
            self.clear(reservation, 'released', '通话／语音保护已结束，可以重新连接其它设备。')

    def close(self):
        self.closed = True
        if self.reservation:
            self.clear(self.reservation)
        for task in tuple(self.tasks):
            task.cancel()

    async def wait_closed(self):
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.tasks.clear()


class GuardedDevice(Device):
    """Reject new ACLs before HCI Accept, including already-paired reconnects."""
    voice_protection = None

    def on_connection_request(self, bd_addr, class_of_device, link_type):
        guard = self.voice_protection
        acl = link_type == hci.HCI_Connection_Complete_Event.LinkType.ACL
        if acl and guard is not None:
            if guard.request_blocked(bd_addr):
                task = asyncio.create_task(self.host.send_async_command(
                    hci.HCI_Reject_Connection_Request_Command(
                        bd_addr=bd_addr, reason=hci.HCI_CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR)))
                guard.tasks.add(task)
                def done(result):
                    guard.tasks.discard(result)
                    if not result.cancelled():
                        result.exception()
                task.add_done_callback(done)
                return
            guard.inflight.add(str(bd_addr))
        return super().on_connection_request(bd_addr, class_of_device, link_type)

    def on_connection_failure(self, transport, peer_address, error_code):
        if self.voice_protection is not None and transport == PhysicalTransport.BR_EDR:
            # Upstream keeps failed incoming accepts in pending_connections.
            # Remove this exact request, without clearing another peer in flight.
            self.voice_protection.inflight.discard(str(peer_address))
        return super().on_connection_failure(transport, peer_address, error_code)
