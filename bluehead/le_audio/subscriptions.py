"""Bond-owned CCCD persistence; no keys, audio, USB or file I/O in callbacks.

GATT 3.3.3.3 requires bonded client configurations to survive reconnects.
Preload before advertising so the first encrypted ATT request sees restored
values. Database and bond fingerprints prevent replay into a different layout
or a newly paired identity that happens to use the same address.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
from bumble import gatt

log = logging.getLogger(__name__)


def bond_digest(keys):
    material = bytearray()
    for name in ('ltk', 'ltk_central', 'ltk_peripheral'):
        key = getattr(keys, name, None)
        value = getattr(key, 'value', b'')
        if isinstance(value, bytes) and len(value) == 16:
            material.extend(name.encode('ascii') + value)
    return hashlib.sha256(material).hexdigest() if material else None


class BondedSubscriptions:
    def __init__(self, cache):
        self.cache, self.app, self.device = cache, cache.app, cache.device
        self.server = cache.server
        self.path = Path(self.app.receiver.directory) / 'gatt-clients.json'
        self.records, self.bonds, self.pairing, self.new_bonds = {}, {}, {}, set()
        self.writes = {}
        self.loaded = self.closed = False
        self.revision = self.saved = 0
        self.writer = None
        if self.server is not None:
            self.server.on(self.server.EVENT_CHARACTERISTIC_SUBSCRIPTION, self.changed)

    def read(self):
        try:
            if self.path.stat().st_size > 262144:
                return {}
            records = json.loads(self.path.read_text('utf-8'))
            if not isinstance(records, dict) or len(records) > 128:
                return {}
            valid = {}
            for key, item in records.items():
                if (not isinstance(key, str) or len(key) != 64 or not isinstance(item, dict)
                        or not isinstance(item.get('bond'), str) or len(item['bond']) != 64
                        or not isinstance(item.get('hash'), str) or len(item['hash']) != 32
                        or type(item.get('service_changed', False)) is not bool):
                    continue
                values = item.get('cccds')
                if (not isinstance(values, dict) or len(values) > 64
                        or any(not isinstance(handle, str) or not handle.isascii() or not handle.isdecimal()
                               or len(handle) > 5 or not 1 <= int(handle) <= 65535
                               or type(value) is not int or not 0 <= value <= 3
                               for handle, value in values.items())):
                    continue
                valid[key] = item
            return valid
        except (OSError, ValueError):
            return {}

    async def load(self):
        if self.loaded:
            return
        records = await asyncio.to_thread(self.read)
        store = getattr(self.device, 'keystore', None)
        get_all = getattr(store, 'get_all', None)
        if get_all:
            for address, keys in (await get_all())[:128]:
                digest = bond_digest(keys)
                if digest:
                    self.bonds[self.identity(address)] = digest
        self.records = records
        # Upgrade the previous Service Changed-only cache without inventing
        # opt-ins for other characteristics or clients that have no bond.
        legacy = await asyncio.to_thread(self.cache.read)
        changed = self.cache.characteristic
        for key, fingerprint in self.bonds.items():
            old = legacy.get(key)
            if key not in self.records and old and len(self.records) < 128:
                values = {str(changed.handle): 2} if changed and old['subscribed'] else {}
                self.records[key] = dict(bond=fingerprint, hash=old['hash'], cccds=values,
                                         service_changed=bool(changed and old['subscribed']))
        self.loaded = True

    def identity(self, address):
        local = str(getattr(self.device, 'public_address', ''))
        return hashlib.sha256((local + '/' + str(address)).encode('utf-8')).hexdigest()

    def restore(self, owner):
        if (self.closed or self.server is None or not self.loaded or id(owner) in self.pairing or not self.app.admitted(owner)
                or not getattr(owner, 'encryption', False)):
            return
        key = self.identity(owner.peer_address)
        record = self.records.get(key)
        if (not record or record['bond'] != self.bonds.get(key)
                or record['hash'] != self.cache.digest(owner)):
            if self.writes.get(id(owner)):
                self.remember(owner)
            return
        live = self.server.subscribers.setdefault(owner, {})
        allowed = {attribute.handle: attribute for attribute in self.server.attributes
                   if isinstance(attribute, gatt.Characteristic)}
        restored = 0
        for handle, value in record['cccds'].items():
            handle = int(handle)
            characteristic = allowed.get(handle)
            if characteristic is not None and self.valid(characteristic, value):
                # A write already received in this connection wins, including
                # an explicit unsubscribe. Restore never emits notifications.
                if handle not in live:
                    live[handle] = value.to_bytes(2, 'little')
                    restored += 1
        if restored:
            log.info('Restored %d bonded GATT client configurations before ATT', restored)
        if self.writes.get(id(owner)):
            # Some generic CCCDs allow writes before encryption. Only commit
            # those writes after this connection proves ownership of the bond.
            self.remember(owner)

    @staticmethod
    def valid(characteristic, value):
        flags = (1 if characteristic.properties & gatt.Characteristic.Properties.NOTIFY else 0)
        flags |= 2 if characteristic.properties & gatt.Characteristic.Properties.INDICATE else 0
        return bool(flags) and not value & ~flags

    def changed(self, bearer, characteristic, notify, indicate):
        owner = getattr(bearer, 'connection', bearer)
        if self.closed or not self.app.admitted(owner):
            return
        value = int(bool(notify)) | (int(bool(indicate)) << 1)
        if not self.valid(characteristic, value):
            return
        wire = value.to_bytes(2, 'little')
        self.writes.setdefault(id(owner), {})[characteristic.handle] = wire
        if id(owner) in self.pairing:
            self.pairing[id(owner)][characteristic.handle] = wire
            return
        if not getattr(owner, 'encryption', False):
            return
        self.remember(owner)

    def pairing_started(self, owner):
        if self.closed:
            return
        pending = {} if getattr(owner, 'encryption', False) else self.writes.get(id(owner), {})
        self.writes[id(owner)] = pending
        self.pairing[id(owner)] = dict(pending)
        self.new_bonds.add(id(owner))

    def paired(self, owner, keys):
        if self.closed or self.server is None:
            return
        key = self.identity(owner.peer_address)
        digest = bond_digest(keys)
        if digest:
            if key not in self.bonds and len(self.bonds) >= 128:
                self.bonds.pop(next(iter(self.bonds)))
            self.bonds[key] = digest
        if id(owner) in self.pairing:
            self.server.subscribers[owner] = self.pairing.pop(id(owner))
        self.remember(owner)

    def service_changed_opt_in(self, owner):
        record = self.records.get(self.identity(owner.peer_address))
        if record and record['bond'] == self.bonds.get(self.identity(owner.peer_address)):
            return record.get('service_changed', False)
        return None

    def remember(self, owner):
        if self.closed or self.server is None:
            return
        key = self.identity(owner.peer_address)
        digest = self.bonds.get(key)
        if digest is None:
            return
        allowed = {attribute.handle: attribute for attribute in self.server.attributes
                   if isinstance(attribute, gatt.Characteristic)}
        values = {}
        for handle, wire in self.server.subscribers.get(owner, {}).items():
            characteristic = allowed.get(handle)
            if characteristic is not None and isinstance(wire, bytes) and len(wire) == 2:
                value = int.from_bytes(wire, 'little')
                if self.valid(characteristic, value):
                    values[str(handle)] = value
        if key not in self.records and len(self.records) >= 128:
            self.records.pop(next(iter(self.records)))
        changed = self.cache.characteristic
        opted_in = bool(changed and values.get(str(changed.handle), 0) & 2)
        record = dict(bond=digest, hash=self.cache.digest(owner), cccds=values, service_changed=opted_in)
        if self.records.get(key) == record:
            return
        self.records[key] = record
        self.revision += 1
        if self.writer is None or self.writer.done():
            self.writer = asyncio.create_task(self.flush())

    def write(self, payload):
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    async def flush(self):
        try:
            while self.saved != self.revision:
                revision = self.revision
                payload = json.dumps(self.records, separators=(',', ':'))
                await asyncio.to_thread(self.write, payload)
                self.saved = revision
        except OSError as error:
            log.warning('Bonded notification configuration could not be saved: %s', type(error).__name__)

    def disconnected(self, owner):
        self.pairing.pop(id(owner), None)
        self.new_bonds.discard(id(owner))
        self.writes.pop(id(owner), None)

    async def close(self):
        self.closed = True
        if self.server is not None:
            self.server.remove_listener(self.server.EVENT_CHARACTERISTIC_SUBSCRIPTION, self.changed)
        if self.writer is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self.writer
        self.pairing.clear()
        self.new_bonds.clear()
        self.writes.clear()
