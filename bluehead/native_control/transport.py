"""Bumble signalling adapter. Physical USB remains in the native runtime."""
import asyncio
import contextlib
from ..controller import transport_target

class _Source:
    def __init__(self, client):
        self.client, self.sink = client, None
        self.failure = None
        self.failure_category = ''
        self.startup = {}
        self.ready = asyncio.Event()
        self.terminated = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.poll())

    def set_packet_sink(self, sink):
        self.sink = sink

    async def poll(self):
        try:
            while True:
                batch = self.client.request("usb_poll")
                self.startup = batch
                if batch.get('starting'):
                    await asyncio.sleep(.04)
                    continue
                if batch.get('error'):
                    self.failure_category = ('firmware_initialization' if batch['error'].startswith('Intel firmware:')
                                             else 'usb_startup')
                    self.failure = OSError(startup_error(batch['error']))
                    break
                if batch['active']:
                    self.ready.set()
                for packet in batch["packets"]:
                    if self.sink:
                        self.sink.on_packet(bytes.fromhex(packet))
                if not batch["active"]:
                    reason = batch.get('reason', '')
                    if reason and reason != 'device_removed':
                        self.failure_category = reason
                        self.failure = ConnectionError(
                            '蓝牙控制队列积压，接收已停止，请重新开始接收' if reason == 'control_backpressure' else
                            '蓝牙传输已停止，请重新开始接收')
                    break
                await asyncio.sleep(.004 if not batch["packets"] else 0)
        except asyncio.CancelledError:
            return
        except (OSError, ValueError) as error:
            self.failure = error
            self.failure_category = 'native_channel'
        finally:
            self.ready.set()
            if not self.terminated.done():
                self.terminated.set_result(None)


def startup_error(detail):
    """Keep the actual firmware failure visible and suggest a useful remedy."""
    if 'driver operation is in progress' in detail:
        return '正在更换或恢复蓝牙驱动，请等待操作完成后再开始接收'
    if 'already has an HCI owner' in detail:
        return '这个蓝牙适配器正在被另一个接收进程使用，请先停止其它接收器'
    if not detail.startswith('Intel firmware:'):
        return detail
    if 'bundled image' in detail or 'device configuration' in detail or 'signed image header' in detail:
        return 'Intel 蓝牙启动固件缺失或损坏，请重新安装完整 BlueHead 程序。详情：' + detail
    if 'unsupported' in detail or 'no bundled image' in detail:
        return '这个 Intel 蓝牙型号的启动固件暂未适配，可在“蓝牙适配器”恢复原厂驱动。详情：' + detail
    return 'Intel 蓝牙固件初始化未完成，请停止后重新开始接收；仍失败时可恢复原厂驱动。详情：' + detail


class _Sink:
    def __init__(self, client):
        self.client = client

    def on_packet(self, packet):
        self.client.request("usb_send", packet=bytes(packet).hex())


class NativeTransport:
    def __init__(self, client, spec):
        self.client = client
        self.client.request("usb_open", **transport_target(spec))
        self.source, self.sink = _Source(client), _Sink(client)

    async def __aenter__(self):
        return self

    async def wait_ready(self, report=None):
        """Bootstrap runs natively; this wait is cancellable and carries no HCI."""
        previous = None
        messages = {
            'opening': '正在打开蓝牙适配器…',
            'probing': '正在检查蓝牙控制器…',
            'validating': '正在校验蓝牙启动固件…',
            'downloading': '正在加载蓝牙启动固件…',
            'booting': '正在启动蓝牙控制器…',
            'configuring': '正在应用蓝牙配置…',
        }
        while not self.source.ready.is_set():
            state = self.source.startup.get('firmware_state', 'opening')
            if report and state != previous and state in messages:
                report(messages[state])
            previous = state
            try:
                await asyncio.wait_for(self.source.ready.wait(), .1)
            except TimeoutError:
                continue
        if self.source.failure:
            raise self.source.failure
        if self.source.terminated.done():
            raise ConnectionError('蓝牙适配器在启动时断开，请检查连接和专用接收驱动')

    async def __aexit__(self, exc_type, _error, _trace):
        self.source.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.source.task
        try:
            self.client.request("usb_close")
        except OSError:
            if exc_type is None:
                raise
