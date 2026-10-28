"""Per-connection discovery evidence; no audio scheduling or transport owner.

A timeout describes missing evidence, never proves an OEM allowlist rejection.
Configured idle ASEs are successful setup, not a reason to tear down a connection.
"""

import asyncio
from dataclasses import dataclass


RANK = dict(connected=0, encrypted=1, services=2, configured=3, channel_ready=4, streaming=5)
TEXT = dict(connected='已连接低功耗蓝牙，等待配对与加密。',
            encrypted='LE 配对已加密，等待设备读取音频服务。',
            services='设备已读取 LE 音频服务，等待配置 LC3。',
            configured='设备已配置 LC3，等待建立音频通道。',
            channel_ready='LE Audio 音频通道已建立，等待播放。',
            streaming='正在使用 LE Audio / LC3。')


@dataclass
class Attempt:
    owner: object
    le: bool
    phase: str = 'connected'
    timer: object = None
    reason: str = ''
    audio_active: bool = False


class ConnectionProgress:
    def __init__(self, app, *, timeout=15.0):
        self.app, self.timeout = app, timeout
        self.attempts, self.closed = {}, False

    def connected(self, owner, *, le=True):
        if self.closed or id(owner) in self.attempts:
            return
        record = self.attempts[id(owner)] = Attempt(owner, le)
        record.timer = asyncio.get_running_loop().call_later(self.timeout, self.expired, record)
        if le:
            self.mark(owner, 'encrypted' if getattr(owner, 'encryption', False) else 'connected')
        else:
            self.publish(record, 'classic_pending', '当前使用经典蓝牙；仍可由发送设备建立 LE Audio。')

    def current(self, record):
        return not self.closed and self.attempts.get(id(record.owner)) is record

    def publish(self, record, phase, reason, *, code=''):
        if not self.current(record):
            return
        record.reason = reason
        self.app.emit('le_audio_discovery', key=str(record.owner.peer_address), phase=phase,
                      evidence=record.phase, reason=reason, reason_code=code)

    def mark(self, owner, phase):
        record = self.attempts.get(id(owner))
        if (record is None or not record.le or phase not in RANK
                or RANK[phase] < RANK[record.phase] or not self.current(record)):
            return
        if phase == record.phase and record.reason:
            return
        record.phase = phase
        self.publish(record, phase, TEXT[phase])
        if RANK[phase] >= RANK['configured'] and record.timer:
            record.timer.cancel()
            record.timer = None

    def streaming(self, key):
        for record in tuple(self.attempts.values()):
            if record.le and str(record.owner.peer_address) == key:
                if not record.audio_active:
                    record.audio_active = True
                    record.phase = 'streaming'
                    self.publish(record, 'streaming', TEXT['streaming'])
                    if record.timer:
                        record.timer.cancel()
                        record.timer = None

    def idle(self, key):
        for record in tuple(self.attempts.values()):
            if record.le and str(record.owner.peer_address) == key and record.audio_active:
                record.audio_active = False
                self.publish(record, 'ready', 'LE Audio 已就绪，当前没有传来媒体音频。')

    def expired(self, record):
        if not self.current(record):
            return
        record.timer = None
        if record.le and RANK[record.phase] >= RANK['configured']:
            return
        key = str(record.owner.peer_address)
        matches = [r for r in self.attempts.values() if r.le and str(r.owner.peer_address) == key]
        if not record.le and matches:
            # The LE attempt owns the explanation, including a valid idle ASE.
            return
        code = record.phase if record.le else 'no_le_link'
        if not record.le:
            reason = '当前使用经典蓝牙，未发现这台设备的 LE 连接。若要使用 LC3，请打开媒体编码右侧设置中的“LE 配对指南”。'
        elif record.phase == 'connected':
            reason = 'LE 已连接但尚未加密。请在发送设备上完成配对；如仍无音频，按“LE 配对指南”重新配对。'
        elif record.phase == 'encrypted':
            reason = 'LE 已加密，但设备尚未读取音频服务。请按“LE 配对指南”检查设备的 LE Audio 开关与许可名单设置。'
        else:
            reason = '设备已读取音频服务，但尚未配置 LC3。请在发送设备中启用 LE Audio 并播放音频；空闲时不会显示实时码率。'
        self.publish(record, 'waiting_peer', reason, code=code)

    def snapshot(self, key):
        rows = [r for r in self.attempts.values() if str(r.owner.peer_address) == key]
        record = next((r for r in rows if r.le), next(iter(rows), None))
        return {} if record is None else dict(transport='le' if record.le else 'classic',
                                              phase=record.phase, audio_active=record.audio_active, reason=record.reason)

    def disconnected(self, owner):
        record = self.attempts.pop(id(owner), None)
        if record and record.timer:
            record.timer.cancel()

    def close(self):
        self.closed = True
        for record in tuple(self.attempts.values()):
            self.disconnected(record.owner)
