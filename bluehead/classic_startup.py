"""Coordinate optional Classic profile work with an in-progress SSP exchange.

This never authenticates, disconnects, retries a link or owns an HCI handle.
Incoming profiles and audio are not gated. A pairing timeout abandons only
optional local work, leaving the sender's normal connection path available.
"""
import asyncio
import logging
import time

log = logging.getLogger(__name__)


class ClassicStartup:
    def __init__(self, connection):
        self.connection = connection
        self.started = time.monotonic()
        self.pairing = self.closed = self.failed = False
        self.changed = asyncio.Event()
        self.listeners = []
        for event, handler in (
            ('classic_pairing', self.completed),
            ('classic_pairing_failure', self.failure),
            ('connection_authentication', self.completed),
            ('connection_authentication_failure', self.failure),
            ('connection_encryption_change', self.encryption),
            ('disconnection', self.disconnected),
        ):
            connection.on(event, handler)
            self.listeners.append((event, handler))

    def trace(self, stage, reason=None):
        log.info('Classic startup: peer=%s handle=%s stage=%s elapsed_ms=%d authenticated=%s encrypted=%s reason=%s',
                 getattr(self.connection, 'peer_address', ''), getattr(self.connection, 'handle', ''),
                 stage, round((time.monotonic() - self.started) * 1000),
                 bool(getattr(self.connection, 'authenticated', False)),
                 bool(getattr(self.connection, 'encryption', False)), reason)

    def pairing_started(self):
        if self.closed:
            return
        if not self.pairing:
            self.trace('pairing')
        self.pairing, self.failed = True, False
        self.changed.set()

    def completed(self, *_):
        if not self.closed:
            self.pairing = self.failed = False
            self.trace('paired-or-authenticated')
            self.changed.set()

    def encryption(self, *_):
        if getattr(self.connection, 'encryption', False):
            self.completed()

    def failure(self, reason):
        self.pairing, self.failed = False, True
        self.trace('authentication-failed', int(reason))
        self.changed.set()

    def disconnected(self, reason):
        if self.closed:
            return
        self.closed = True
        self.trace('disconnected', int(reason))
        self.changed.set()
        for event, handler in self.listeners:
            self.connection.remove_listener(event, handler)
        self.listeners.clear()

    async def optional_ready(self, timeout=30):
        try:
            async with asyncio.timeout(timeout):
                while self.pairing and not self.closed:
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError:
            self.trace('optional-work-deferred')
            return False
        return not self.closed and not self.failed


async def optional_ready(connection):
    startup = getattr(connection, '_bluehead_classic_startup', None)
    return await startup.optional_ready() if startup else True
