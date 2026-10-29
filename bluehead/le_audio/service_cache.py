"""Bonded GATT client configuration and confirmed database invalidation.

Preload notification opt-ins before advertising; disk writes stay outside HCI
callbacks. Layout and bond fingerprints prevent replay across identities or
re-pairing. Service Changed is only sent to opted-in, bonded encrypted peers;
failed confirmation retains the old database hash for the next connection.
"""

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
from .subscriptions import BondedSubscriptions


class ServiceCache:
    def __init__(self, app):
        self.app, self.device = app, app.device
        self.path = Path(app.receiver.directory) / 'gatt-cache.json'
        self.records, self.tasks, self.checked = {}, set(), set()
        self.lock = asyncio.Lock()
        self.closed = False
        self.characteristic = getattr(getattr(self.device, 'gatt_service', None),
                                      'service_changed_characteristic', None)
        self.server = getattr(self.device, 'gatt_server', None)
        if self.characteristic is not None:
            self.characteristic.on('subscription', self.subscription)
        self.subscriptions = BondedSubscriptions(self)

    async def load(self):
        await self.subscriptions.load()

    def pairing_started(self, owner):
        self.subscriptions.pairing_started(owner)

    def paired(self, owner, keys):
        self.subscriptions.paired(owner, keys)
        self.checked.discard(id(owner))

    def identity(self, owner):
        local = str(getattr(self.device, 'public_address', ''))
        return hashlib.sha256((local + '/' + str(owner.peer_address)).encode('utf-8')).hexdigest()

    def digest(self, owner):
        return self.device.gatt_service.get_database_hash(owner).hex()

    def spawn(self, operation):
        if self.closed:
            operation.close()
            return
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        task.add_done_callback(lambda value: (self.tasks.discard(value),
                                             None if value.cancelled() else value.exception()))

    async def bonded(self, owner):
        store = getattr(self.device, 'keystore', None)
        return bool(store and await store.get(str(owner.peer_address)))

    def subscription(self, bearer, notify, indicate):
        owner = getattr(bearer, 'connection', bearer)
        if self.app.admitted(owner) and getattr(owner, 'encryption', False):
            self.spawn(self.remember(owner, bool(indicate)))

    def ready(self, owner):
        self.subscriptions.restore(owner)
        if (self.characteristic is not None and self.app.admitted(owner)
                and getattr(owner, 'encryption', False) and id(owner) not in self.checked):
            self.checked.add(id(owner))
            self.spawn(self.reconcile(owner))

    def read(self):
        try:
            if self.path.stat().st_size > 65536:
                return {}
            value = json.loads(self.path.read_text('utf-8'))
            if not isinstance(value, dict) or len(value) > 128:
                return {}
            return {key: item for key, item in value.items()
                    if isinstance(key, str) and len(key) == 64 and isinstance(item, dict)
                    and type(item.get('subscribed')) is bool and isinstance(item.get('hash'), str)
                    and len(item['hash']) == 32}
        except (OSError, ValueError):
            return {}

    def write(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.records, separators=(',', ':')), encoding='utf-8')
        os.replace(temporary, self.path)

    async def remember(self, owner, subscribed):
        try:
            async with self.lock:
                self.records = await asyncio.to_thread(self.read)
                if not await self.bonded(owner) or not self.app.admitted(owner):
                    return
                key = self.identity(owner)
                if key not in self.records and len(self.records) >= 128:
                    self.records.pop(next(iter(self.records)))
                self.records[key] = dict(subscribed=subscribed, hash=self.digest(owner))
                await asyncio.to_thread(self.write)
        except OSError:
            # An unwritable optional cache must not break audio or pairing.
            pass

    async def reconcile(self, owner):
        try:
            async with self.lock:
                self.records = await asyncio.to_thread(self.read)
                if (not await self.bonded(owner) or not self.app.admitted(owner)
                        or id(owner) in self.subscriptions.new_bonds):
                    return
                record = self.records.get(self.identity(owner))
                if (not record or not record['subscribed']
                        or self.subscriptions.service_changed_opt_in(owner) is False):
                    return
                live = self.server.subscribers.setdefault(owner, {})
                live.setdefault(self.characteristic.handle, b'\x02\0')
                if not live[self.characteristic.handle][0] & 2:
                    return
                digest = self.digest(owner)
                if record['hash'] != digest:
                    await asyncio.wait_for(self.device.indicate_subscriber(
                        owner, self.characteristic, b'\x01\0\xff\xff'), 3)
                    if self.app.admitted(owner):
                        record['hash'] = digest
                        await asyncio.to_thread(self.write)
        except Exception:
            if self.app.admitted(owner):
                self.app.emit('le_audio_discovery', key=str(owner.peer_address), phase='cache_pending',
                              reason='设备尚未确认更新后的音频服务。如仍没有 LE Audio 选项，请在设备蓝牙设置中忽略 BlueHead 后重新配对。')

    def disconnected(self, owner):
        self.checked.discard(id(owner))
        self.subscriptions.disconnected(owner)

    async def close(self):
        self.closed = True
        await self.subscriptions.close()
        if self.characteristic is not None:
            self.characteristic.remove_listener('subscription', self.subscription)
        for task in tuple(self.tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.checked.clear()
