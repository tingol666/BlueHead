"""Windows-owned A2DP sink control. PCM never passes through Python."""
import asyncio
import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import threading
import uuid

log = logging.getLogger(__name__)
CONNECTION_PROPERTIES = ['System.Devices.Aep.IsConnected', 'System.Devices.Connected', 'System.Devices.ContainerId']


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    windows_connected: bool | None = None


def connection_property(info):
    properties = getattr(info, 'properties', {})
    for key in CONNECTION_PROPERTIES[:2]:
        value = properties.get(key)
        if type(value) is bool:
            return value
    return None


def source_key(device_id):
    return 'windows:' + hashlib.sha256(device_id.encode('utf-8')).hexdigest()[:24]


def system_radio_available():
    """Check a Windows-owned radio before entering the WinRT audio API.

    WinUSB adapters are intentionally absent here. Device enumeration with no
    Windows radio can hang inside the OS and its late async completion can outlive
    the COM apartment. Never start that operation just to discover this state.
    """
    class Params(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD)]
    api = ctypes.WinDLL('bthprops.cpl')
    kernel = ctypes.WinDLL('kernel32')
    api.BluetoothFindFirstRadio.argtypes = [ctypes.POINTER(Params), ctypes.POINTER(wintypes.HANDLE)]
    api.BluetoothFindFirstRadio.restype = wintypes.HANDLE
    api.BluetoothFindRadioClose.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    params, radio = Params(ctypes.sizeof(Params)), wintypes.HANDLE()
    search = api.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
    if not search:
        return False
    try:
        return bool(radio.value)
    finally:
        if radio.value:
            kernel.CloseHandle(radio)
        api.BluetoothFindRadioClose(search)


class WindowsPlayback:
    def __enter__(self):
        if not system_radio_available():
            raise OSError('Windows 蓝牙尚未就绪。请先在“蓝牙驱动设置”恢复 Windows 蓝牙，再点击“重新检测”。')
        import winrt.runtime
        import winrt.windows.foundation
        import winrt.windows.foundation.collections
        from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind
        from winrt.windows.media.audio import AudioPlaybackConnection
        self.runtime = winrt.runtime
        self.runtime.init_apartment(self.runtime.ApartmentType.MULTI_THREADED)
        self.information, self.connection = DeviceInformation, AudioPlaybackConnection
        self.container_kind = DeviceInformationKind.DEVICE_CONTAINER
        self.watcher, self.tokens = None, []
        return self

    async def discover(self):
        if not system_radio_available():
            raise OSError('Windows 蓝牙尚未就绪。请在“蓝牙驱动设置”重新检测并恢复 Windows 蓝牙。')
        infos = await self.information.find_all_async(self.connection.get_device_selector(), CONNECTION_PROPERTIES)
        # AudioPlaybackConnection enumerates service interfaces, not AEPs. The
        # connected bit may only be present on the associated device container.
        # Resolve each container once, without creating any Bluetooth object or
        # opening an audio connection. Missing metadata stays unknown, not true.
        containers = {}
        async def state(info):
            value = connection_property(info)
            if value is not None:
                return value
            try:
                container = '{' + str(uuid.UUID(str(info.properties.get('System.Devices.ContainerId')))) + '}'
            except (ValueError, TypeError, AttributeError):
                return None
            if container not in containers:
                async def read():
                    try:
                        details = await asyncio.wait_for(self.information.create_from_id_async(
                            container, ['System.Devices.Connected'], self.container_kind), 2)
                        return connection_property(details)
                    except (OSError, RuntimeError, TimeoutError):
                        return None
                containers[container] = asyncio.create_task(read())
            return await containers[container]
        states = await asyncio.gather(*(state(info) for info in infos))
        return [Source(info.id, info.name, connected) for info, connected in zip(infos, states)]

    def create(self, device_id):
        return self.connection.try_create_from_id(device_id)

    def watch(self, changed):
        self.watcher = self.information.create_watcher(self.connection.get_device_selector(), CONNECTION_PROPERTIES)
        for event in ('added', 'removed', 'updated'):
            self.tokens.append((event, getattr(self.watcher, 'add_' + event)(lambda *_: changed())))
        self.watcher.start()

    def __exit__(self, *_):
        if self.watcher:
            with contextlib.suppress(Exception):
                self.watcher.stop()
            for event, token in self.tokens:
                with contextlib.suppress(Exception):
                    getattr(self.watcher, 'remove_' + event)(token)
        self.runtime.uninit_apartment()


def selected_sources(directory):
    if directory is None:
        return set()
    try:
        data = json.loads((Path(directory) / 'windows-audio-selections.json').read_text('utf-8'))
        values = data['sources'] if data.get('schema') == 1 else []
        if not isinstance(values, list) or len(values) > 128:
            return set()
        return {key for key in values if isinstance(key, str) and key.startswith('windows:') and len(key) == 32}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return set()


def save_source_choices(directory, keys):
    path = Path(directory) / 'windows-audio-selections.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'schema': 1, 'sources': sorted(keys)[:128]}), 'utf-8')
    temporary.replace(path)


async def scan_sources(platform_factory=WindowsPlayback, *, directory=None, stop_key=None):
    """Readonly discovery while stopped; never creates or opens a connection."""
    with platform_factory() as platform:
        sources = await asyncio.wait_for(platform.discover(), 10)
        selected = selected_sources(directory)
        if stop_key is not None and directory is not None:
            selected.discard(stop_key)
            save_source_choices(directory, selected)
        return [dict(key=source_key(info.id), name=info.name, state='selected' if source_key(info.id) in selected else 'available',
                     selected=source_key(info.id) in selected,
                     windows_connected=getattr(info, 'windows_connected', None),
                     detail='已配对 · 点击“接收此设备的音频”才为此设备准备媒体服务；Windows 配对保留。') for info in sources]


class WindowsControlActor:
    """One process-lifetime MTA for discovery and every playback connection.

    Keep a base apartment reference while WinRT factories/delegates are cached.
    Receiver sessions may stop and restart without moving COM objects between
    temporary discovery and receiver threads. This actor never handles PCM/HCI.
    """
    def __init__(self):
        self.guard, self.ready = threading.RLock(), threading.Event()
        self.thread, self.loop, self.error = None, None, None
        self.closed = False

    def _worker(self):
        runtime, initialized = None, False
        try:
            import winrt.runtime as runtime
            runtime.init_apartment(runtime.ApartmentType.MULTI_THREADED)
            initialized = True
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.ready.set()
            self.loop.run_forever()
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
        except Exception as error:
            self.error = error
            self.ready.set()
        finally:
            if initialized:
                runtime.uninit_apartment()

    def submit(self, coroutine):
        try:
            with self.guard:
                if self.closed:
                    raise OSError('Windows 音频控制已关闭')
                if self.thread is None:
                    self.thread = threading.Thread(target=self._worker, daemon=True, name='windows-audio-control')
                    self.thread.start()
                if not self.ready.wait(10) or self.error or self.loop is None or self.loop.is_closed():
                    raise OSError('Windows 音频控制未能准备，请重新打开软件') from self.error
                return asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            coroutine.close()
            raise

    async def invoke(self, coroutine):
        return await asyncio.wrap_future(self.submit(coroutine))

    async def run_receiver(self, receiver, stop):
        actor_stop, abort = [], threading.Event()
        async def run():
            local_stop = asyncio.Event()
            actor_stop.append(local_stop)
            if stop.is_set() or abort.is_set():
                local_stop.set()
            await receiver.run(local_stop)
        def request_stop():
            abort.set()
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(lambda: actor_stop and actor_stop[0].set())
        future = asyncio.wrap_future(self.submit(run()))
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((future, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                request_stop()
            await asyncio.shield(future)
        finally:
            request_stop()
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            if not future.done():
                await asyncio.shield(future)

    def close(self):
        with self.guard:
            if self.closed:
                return
            self.closed = True
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(8)
            if self.thread.is_alive():
                raise OSError('Windows 音频控制仍在退出')


@dataclass
class Playback:
    connection: object
    token: object = None
    state: str = 'listening'
    detail: str = '等待设备发送声音'
    opened: bool = False


class NativeReceiver:
    def __init__(self, emit, *, directory=None, auto_reconnect=False, platform_factory=WindowsPlayback):
        self.emit, self.platform_factory = emit, platform_factory
        self.directory = Path(directory) if directory is not None else None
        self.auto_reconnect = auto_reconnect
        self.connections, self.sources, self.operations, self.locks = {}, {}, {}, {}
        self.refresh_lock, self.refresh_event = asyncio.Lock(), asyncio.Event()
        self.closed, self.loop, self.stop = False, None, None
        self.manual_disconnects, self.reconnect_keys = set(), selected_sources(self.directory)
        self.retry_counts = {}
        self.retry_waiters = set()
        # The old reconnect file included choices inferred from incoming
        # connections. It is deliberately not migrated into explicit consent.

    def snapshot(self):
        rows = []
        for key, info in self.sources.items():
            entry = self.connections.get(key)
            rows.append(dict(key=key, name=info.name, state=entry.state if entry else 'available',
                             selected=key in self.reconnect_keys,
                             windows_connected=getattr(info, 'windows_connected', None),
                             detail=entry.detail if entry else '已配对 · 点击“接收此设备的音频”才交给 BlueHead。Windows 配对保留。'))
        self.emit('shared_devices', devices=rows, running=not self.closed, scanning=False)

    def changed(self):
        if not self.closed and self.loop and not self.loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.refresh_event.set)

    async def bounded(self, operation, seconds=12):
        task, stopped = asyncio.ensure_future(operation), asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait((task, stopped), timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
            if self.stop.is_set() or self.closed:
                raise asyncio.CancelledError
            if task in done:
                return task.result()
            raise TimeoutError('Windows 音频连接暂未响应')
        finally:
            for pending in (task, stopped):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)

    def remember(self, key, selected=True):
        if selected:
            self.reconnect_keys.add(key)
        else:
            self.reconnect_keys.discard(key)
        if self.directory:
            try:
                save_source_choices(self.directory, self.reconnect_keys)
            except OSError:
                log.warning('Cannot save Windows reconnect choices', exc_info=True)

    def state_changed(self, key, entry):
        if self.closed or self.connections.get(key) is not entry or key not in self.sources:
            return
        # ConnectionState.Opened == 1. OpenResultStatus is a DIFFERENT enum.
        opened, was_opened = int(entry.connection.state) == 1, entry.opened
        entry.opened = opened
        if opened:
            entry.state, entry.detail = 'connected', '音频已连接 · 播放设备与音量由 Windows 管理'
            self.manual_disconnects.discard(key)
            self.retry_counts.pop(key, None)
            if not was_opened:
                self.emit('device', key=key, state='已连接')
                self.emit('name', key=key, name=self.sources[key].name)
        elif was_opened:
            entry.state, entry.detail = 'listening', '音频已断开 · 可重新连接'
            self.emit('device', key=key, state='已断开')
            if self.auto_reconnect and key not in self.manual_disconnects:
                self.schedule_reconnect(key)
        self.snapshot()

    def subscribe(self, key, entry):
        def changed(*_):
            if self.loop and not self.loop.is_closed():
                with contextlib.suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(self.state_changed, key, entry)
        entry.token = entry.connection.add_state_changed(changed)

    def retire(self, key):
        entry = self.connections.pop(key, None)
        if entry:
            if entry.token is not None:
                with contextlib.suppress(Exception):
                    entry.connection.remove_state_changed(entry.token)
            with contextlib.suppress(Exception):
                entry.connection.close()
            if entry.opened:
                self.emit('device', key=key, state='已断开')

    async def enable(self, key):
        if key in self.connections or key not in self.sources or self.closed:
            return self.connections.get(key)
        connection = self.platform.create(self.sources[key].id)
        if connection is None:
            return None
        entry = Playback(connection)
        self.connections[key] = entry
        try:
            self.subscribe(key, entry)
            await self.bounded(connection.start_async())
            self.state_changed(key, entry)
            return entry
        except BaseException:
            self.retire(key)
            raise

    async def refresh(self):
        async with self.refresh_lock:
            infos = await self.bounded(self.platform.discover(), 10)
            latest = {source_key(info.id): info for info in infos}
            for key in set(self.sources) - latest.keys():
                await self.disconnect(key, manual=False)
            self.sources = latest
            self.snapshot()
            for key in tuple(self.sources):
                if key not in self.reconnect_keys or key in self.manual_disconnects:
                    continue
                try:
                    async with self.locks.setdefault(key, asyncio.Lock()):
                        await self.enable(key)  # accepts mobile connections; does not actively open
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning('Cannot enable Windows audio source', exc_info=True)
                if self.auto_reconnect and key not in self.manual_disconnects and key in self.reconnect_keys and not self.connections.get(key, Playback(None)).opened:
                    self.schedule_reconnect(key)

    def schedule_reconnect(self, key):
        if key in self.operations or self.retry_counts.get(key, 0) >= 3 or self.closed:
            return
        attempt = self.retry_counts.get(key, 0)
        self.retry_counts[key] = attempt + 1
        self.retry_waiters.add(key)
        async def retry():
            try:
                await asyncio.sleep((2, 5, 15)[attempt])
                self.retry_waiters.discard(key)
                if key not in self.manual_disconnects:
                    await self.connect(key, retry=True)
            finally:
                if self.operations.get(key) is asyncio.current_task():
                    self.retry_waiters.discard(key)
                    self.operations.pop(key, None)
        self.operations[key] = asyncio.create_task(retry())

    async def connect(self, key, *, retry=False):
        if self.closed:
            return
        if key not in self.sources:
            self.emit('shared_status', message='设备列表已变化，请点击“重新检测”后选择设备。')
            return
        if retry and key not in self.reconnect_keys:
            return
        if not retry:
            self.remember(key)
        pending = self.operations.get(key)
        if pending and pending is not asyncio.current_task():
            if not retry and key in self.retry_waiters:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                self.retry_waiters.discard(key)
                if self.operations.get(key) is pending:
                    self.operations.pop(key, None)
            else:
                return
        current = asyncio.current_task()
        self.operations[key] = current
        self.manual_disconnects.discard(key)
        try:
            async with self.locks.setdefault(key, asyncio.Lock()):
                if self.connections.get(key, Playback(None)).opened:
                    return
                # A failed Open must never become an unretryable cached entry.
                self.retire(key)
                entry = await self.enable(key)
                if entry is None:
                    self.emit('shared_status', message='此设备当前没有可用的媒体音频连接，请在 Windows 中检查配对。')
                    return
                entry.state, entry.detail = 'connecting', '正在连接音频…'
                self.snapshot()
                result = await self.bounded(entry.connection.open_async())
                status = int(result.status)
                if status == 0:
                    self.state_changed(key, entry)
                    if not entry.opened:
                        entry.state, entry.detail = 'listening', '连接请求已接受 · 等待设备打开音频'
                else:
                    detail = {1: '连接超时，请确认设备已开启蓝牙后重试',
                              2: 'Windows 暂未允许连接，请检查系统蓝牙与设备配对',
                              3: '音频连接未成功，可点击“连接音频”重试'}.get(status, '音频连接未成功，请重试')
                    entry.state, entry.detail = 'failed', detail
                    log.warning('Windows audio Open failed: source=%s status=%d extended_error=%s',
                                key, status, getattr(result, 'extended_error', None))
                self.snapshot()
        except asyncio.CancelledError:
            self.retire(key)
            raise
        except Exception:
            log.warning('Windows audio connection failed: source=%s', key, exc_info=True)
            self.retire(key)
            self.emit('shared_status', message='Windows 音频连接暂未响应，请重新检测后再连接。')
            self.snapshot()
        finally:
            if self.operations.get(key) is current:
                self.operations.pop(key, None)
        if retry and not self.closed and key in self.sources and not self.connections.get(key, Playback(None)).opened:
            self.schedule_reconnect(key)

    async def disconnect(self, key, *, manual=True):
        if manual:
            self.manual_disconnects.add(key)
            self.remember(key, False)
        task = self.operations.pop(key, None)
        self.retry_waiters.discard(key)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.locks.setdefault(key, asyncio.Lock()):
            self.retire(key)
        self.snapshot()

    async def run(self, stop):
        self.stop, self.loop = stop, asyncio.get_running_loop()
        with self.platform_factory() as self.platform:
            if stop.is_set():
                return
            try:
                try:
                    self.platform.watch(self.changed)
                except Exception:
                    # Enumeration still works on systems where DeviceWatcher
                    # is temporarily unavailable. Manual refresh remains live.
                    log.warning('Windows audio watcher unavailable; using periodic discovery', exc_info=True)
                announced = False
                while not stop.is_set():
                    self.refresh_event.clear()
                    try:
                        await self.refresh()
                        if not announced:
                            announced = True
                            self.emit('shared_ready')
                            self.emit('status', message='已就绪：在设备列表选择设备，点击“接收此设备的音频”，再播放声音。')
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        log.warning('Windows audio discovery temporarily failed', exc_info=True)
                        self.emit('shared_status', message='正在重新检测 Windows 蓝牙设备…')
                    changed, stopped = asyncio.create_task(self.refresh_event.wait()), asyncio.create_task(stop.wait())
                    try:
                        await asyncio.wait((changed, stopped), timeout=15, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        changed.cancel()
                        stopped.cancel()
                        await asyncio.gather(changed, stopped, return_exceptions=True)
            finally:
                self.closed = True
                tasks = {task for task in self.operations.values() if task is not asyncio.current_task()}
                for task in tasks:
                    task.cancel()
                for key in tuple(self.connections):
                    self.retire(key)
                await asyncio.gather(*tasks, return_exceptions=True)
                self.operations.clear()
                self.retry_waiters.clear()
                self.snapshot()
