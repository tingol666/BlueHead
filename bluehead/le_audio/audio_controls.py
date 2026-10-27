"""Bounded encrypted VCS/MICS controls for the existing native LE audio graph.

Standard renderer volume (0..255) is separate from local per-device 0..200%
gain. One VCS renderer has one shared state, as required by VCS 3.1.4. Phone
mute cannot override the application's local microphone privacy switch.
"""

import asyncio
import contextlib
from bumble import att, gatt

READ = gatt.Characteristic.Properties.READ
WRITE = gatt.Characteristic.Properties.WRITE
NOTIFY = gatt.Characteristic.Properties.NOTIFY
READ_SECURE = gatt.Characteristic.Permissions.READABLE | gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION
WRITE_SECURE = (
    gatt.Characteristic.Permissions.WRITEABLE | gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION
)


class AudioControls:
    def __init__(self, device, render, microphone=None, *, private=lambda: False):
        self.device, self.render, self.microphone, self.private = device, render, microphone, private
        self.admit = lambda owner: True
        self.level, self.muted, self.counter, self.flags = 255, 0, 0, 0
        self.microphone_muted = False
        self.lock, self.writers = asyncio.Lock(), set()
        self.pending, self.closed = 0, False
        self.volume = gatt.Characteristic(
            gatt.GATT_VOLUME_STATE_CHARACTERISTIC,
            READ | NOTIFY,
            READ_SECURE,
            gatt.CharacteristicValue(read=self.read_volume),
        )
        self.control = gatt.Characteristic(
            gatt.GATT_VOLUME_CONTROL_POINT_CHARACTERISTIC,
            WRITE,
            WRITE_SECURE,
            gatt.CharacteristicValue(write=self.write_volume),
        )
        self.volume_flags = gatt.Characteristic(
            gatt.GATT_VOLUME_FLAGS_CHARACTERISTIC,
            READ | NOTIFY,
            READ_SECURE,
            gatt.CharacteristicValue(read=self.read_flags),
        )
        self.services = [
            gatt.Service(gatt.GATT_VOLUME_CONTROL_SERVICE, [self.volume, self.control, self.volume_flags])
        ]
        self.mic = None
        if microphone is not None:
            self.mic = gatt.Characteristic(
                gatt.GATT_MUTE_CHARACTERISTIC,
                READ | WRITE | NOTIFY,
                READ_SECURE | WRITE_SECURE,
                gatt.CharacteristicValue(read=self.read_microphone, write=self.write_microphone),
            )
            self.services.append(gatt.Service(gatt.GATT_MICROPHONE_CONTROL_SERVICE, [self.mic]))

    def current(self, owner):
        return not self.closed and self.admit(owner) and any(p is owner for p in self.device.connections.values())

    def require(self, owner):
        if not self.current(owner) or not getattr(owner, "encryption", False):
            raise att.ATT_Error(att.ATT_INSUFFICIENT_ENCRYPTION_ERROR)

    def read_volume(self, owner):
        self.require(owner)
        return bytes([self.level, self.muted, self.counter])

    def read_flags(self, owner):
        self.require(owner)
        return bytes([self.flags])

    def mic_state(self):
        return 2 if self.private() else int(self.microphone_muted)

    def read_microphone(self, owner):
        self.require(owner)
        return bytes([self.mic_state()])

    async def notify(self, characteristic, value):
        # Include subscribers that have only written a CCCD, not just readers.
        for owner in tuple(self.device.connections.values()):
            if self.current(owner) and getattr(owner, "encryption", False):
                with contextlib.suppress(OSError, ConnectionError):
                    await self.device.notify_subscriber(owner, characteristic, value)

    async def local_microphone_changed(self):
        if self.mic is not None and not self.closed:
            await self.notify(self.mic, bytes([self.mic_state()]))

    async def write(self, owner, apply):
        self.require(owner)
        if self.pending >= 8:
            raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR)
        task = asyncio.current_task()
        self.pending += 1
        self.writers.add(task)
        try:
            async with self.lock:
                self.require(owner)
                await apply()
        except OSError:
            raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR) from None
        finally:
            self.pending -= 1
            self.writers.discard(task)

    async def write_volume(self, owner, value):
        self.require(owner)
        if not value:
            raise att.ATT_Error(att.ATT_INVALID_ATTRIBUTE_LENGTH_ERROR)
        opcode = value[0]
        if opcode > 6:
            raise att.ATT_Error(0x81)
        if len(value) != (3 if opcode == 4 else 2):
            raise att.ATT_Error(att.ATT_INVALID_ATTRIBUTE_LENGTH_ERROR)

        async def apply():
            if value[1] != self.counter:
                raise att.ATT_Error(0x80)
            level, muted = self.level, self.muted
            if opcode in (0, 2):
                level = max(0, level - 16)
            elif opcode in (1, 3):
                level = min(255, level + 16)
            elif opcode == 4:
                level = value[2]
            if opcode in (2, 3, 5):
                muted = 0
            elif opcode == 6:
                muted = 1
            if (level, muted) == (self.level, self.muted):
                return
            old = (self.level / 255) * (1 - self.muted)
            try:
                await self.render((level / 255) * (1 - muted))
            except BaseException:
                with contextlib.suppress(Exception):
                    await self.render(old)
                raise
            if not self.current(owner) or not getattr(owner, "encryption", False):
                await self.render(old)
                self.require(owner)
            flags_changed = level != self.level and not self.flags
            self.flags |= int(level != self.level)
            self.level, self.muted, self.counter = level, muted, (self.counter + 1) & 255
            await self.notify(self.volume, bytes([level, muted, self.counter]))
            if flags_changed:
                await self.notify(self.volume_flags, bytes([self.flags]))

        await self.write(owner, apply)

    async def write_microphone(self, owner, value):
        self.require(owner)
        if len(value) != 1:
            raise att.ATT_Error(att.ATT_INVALID_ATTRIBUTE_LENGTH_ERROR)
        if value[0] > 1:
            raise att.ATT_Error(0x13)

        async def apply():
            if self.private():
                raise att.ATT_Error(0x80)
            muted = bool(value[0])
            if muted == self.microphone_muted:
                return
            try:
                await self.microphone(muted)
            except BaseException:
                with contextlib.suppress(Exception):
                    await self.microphone(self.microphone_muted)
                raise
            if not self.current(owner) or not getattr(owner, "encryption", False):
                await self.microphone(self.microphone_muted)
                self.require(owner)
            self.microphone_muted = muted
            await self.notify(self.mic, bytes([self.mic_state()]))

        await self.write(owner, apply)

    async def close(self):
        self.closed = True
        current = asyncio.current_task()
        pending = self.writers - {current}
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
