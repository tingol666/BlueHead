"""Bumble signalling adapter. Physical USB remains in the native runtime."""

import asyncio
import contextlib
import logging
from ..controller import transport_target


class ControllerStartupError(OSError):
    def __init__(self, batch):
        self.category = batch.get("firmware_error") or "controller_startup"
        if self.category == "controller_startup" and "timeout at command acknowledgement" in batch.get(
            "error", ""
        ):
            self.category = "controller_unresponsive"
        self.firmware_profile = batch.get("firmware_profile", "")
        super().__init__(startup_error(batch.get("error", "Controller initialization failed")))


class ControllerTransportError(ConnectionError):
    def __init__(self, batch):
        self.category = batch.get("reason") or "usb_transport"
        self.firmware_profile = batch.get("firmware_profile", "")
        self.diagnostics = batch.get("transport", {})
        messages = {
            "controller_command_failed": "蓝牙控制器未能完成指令，连接已清除。请停止后重新开始接收",
            "iso_flow_control": "蓝牙音频传输状态异常，接收已停止。请重新开始接收",
            "controller_hardware_error": "蓝牙控制器报告故障，连接已清除。请重新初始化适配器后再开始接收",
            "controller_restarted": "蓝牙控制器已复位，连接已清除。请重新初始化适配器后再开始接收",
            "control_backpressure": "蓝牙控制队列积压，接收已停止，请重新开始接收",
            "usb_callback_failed": "蓝牙传输处理异常，连接已清除，请重新开始接收",
            "device_removed": "蓝牙适配器已断开，请重新连接适配器后开始接收",
        }
        super().__init__(messages.get(self.category, "蓝牙传输已中断，连接已清除，请重新开始接收"))


def dispatch_packet(host, packet, epoch):
    if hasattr(host, "hci_metadata"):
        host.hci_metadata["bluehead_link_epoch"] = epoch
    host.on_packet(packet)
    handle = None
    if len(packet) >= 7 and packet[0] == 4:
        if packet[1] == 3 and not packet[3]:
            handle = int.from_bytes(packet[4:6], "little")
        elif packet[1] == 0x3E and packet[3] in (1, 0x0A, 0x29) and not packet[4]:
            handle = int.from_bytes(packet[5:7], "little")
    if handle is not None:
        connection = getattr(host, "connections", {}).get(handle)
        if connection is not None:
            # Extended advertising may defer Device's connection callback until
            # a later Advertising Set Terminated event. Keep the original tag
            # on the existing Host connection, not that unrelated later packet.
            connection._bluehead_link_epoch = epoch


class _Source:
    def __init__(self, client, *, guard_commands=False, capture=None):
        self.client, self.sink = client, None
        self.capture = capture
        self.guard_commands, self.command_owner = guard_commands, None
        self.failure = None
        self.failure_category = ""
        self.startup = {}
        self.ready = asyncio.Event()
        self.terminated = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.poll())

    def set_packet_sink(self, sink):
        self.sink = sink
        if self.guard_commands and hasattr(sink, "_send_command") and self.command_owner is None:
            from .command_owner import CommandOwner

            def failed(error):
                self.failure = ConnectionError("蓝牙控制指令通道已中断")
                self.failure_category = "native_channel"

            self.command_owner = CommandOwner(sink, failed)

    async def poll(self):
        try:
            while True:
                if self.failure is not None:
                    break
                batch = self.client.request("usb_poll")
                if self.capture:
                    self.capture.batch(batch)
                self.startup = batch
                if batch.get("starting"):
                    await asyncio.sleep(0.04)
                    continue
                if batch.get("error"):
                    if batch.get("startup_complete"):
                        self.failure = ControllerTransportError(batch)
                        self.failure_category = self.failure.category
                        break
                    self.failure_category = (
                        "firmware_initialization"
                        if batch["error"].startswith(("Intel firmware:", "Firmware:"))
                        else "usb_startup"
                    )
                    self.failure = ControllerStartupError(batch)
                    break
                if batch["active"]:
                    self.ready.set()
                epochs = batch.get("packet_epochs", ())
                for index, packet in enumerate(batch["packets"]):
                    if self.capture:
                        self.capture.packet(bytes.fromhex(packet), 1)
                    if self.sink:
                        dispatch_packet(
                            self.sink, bytes.fromhex(packet), epochs[index] if index < len(epochs) else 0
                        )
                if not batch["active"]:
                    if batch.get("reason") != "device_removed":
                        self.failure = ControllerTransportError(batch)
                        self.failure_category = self.failure.category
                    break
                await asyncio.sleep(0.004 if not batch["packets"] else 0)
        except asyncio.CancelledError:
            return
        except (OSError, ValueError) as error:
            self.failure = error
            self.failure_category = "native_channel"
        finally:
            if self.command_owner is not None:
                self.command_owner.close()
            self.ready.set()
            # Inform the existing signalling owner as well as Receiver.run.
            # This releases pending command waits; cleanup callback failures
            # must not suppress transport termination or leave stale cards.
            if self.sink is not None and hasattr(self.sink, "on_transport_lost"):
                try:
                    self.sink.on_transport_lost()
                except Exception as error:
                    logging.getLogger(__name__).warning(
                        "Transport-loss listener failed (%s)", type(error).__name__
                    )
            if not self.terminated.done():
                self.terminated.set_result(None)


def startup_error(detail):
    """Keep the actual firmware failure visible and suggest a useful remedy."""
    if "driver operation is in progress" in detail:
        return "正在更换或恢复蓝牙驱动，请等待操作完成后再开始接收"
    if "already has an HCI owner" in detail:
        return "这个蓝牙适配器正在被另一个接收进程使用，请先停止其它接收器"
    if detail.startswith("Firmware:"):
        return (
            "蓝牙控制器尚未就绪。请打开“蓝牙驱动设置”，按页面提示获取启动文件，或点击右下方的“修复专用蓝牙”。详情："
            + detail
        )
    if not detail.startswith("Intel firmware:"):
        return detail
    if "bundled image" in detail or "device configuration" in detail or "signed image header" in detail:
        return (
            "蓝牙启动文件缺失或校验失败。请打开“蓝牙驱动设置”，按页面提示重新获取或从原厂驱动包导入。详情："
            + detail
        )
    if "unsupported" in detail or "no bundled image" in detail:
        return (
            "这个适配器的启动文件暂不可用。请打开“蓝牙驱动设置”，点击“修复专用蓝牙”，或“恢复 Windows 蓝牙”后使用基础模式。详情："
            + detail
        )
    return (
        "蓝牙初始化未完成。请打开“蓝牙驱动设置”，点击右下方的“修复专用蓝牙”；若设备被占用，请退出其他蓝牙程序，仍失败时重启电脑后重试。详情："
        + detail
    )


class _Sink:
    def __init__(self, client, capture=None):
        self.client = client
        self.capture = capture

    def on_packet(self, packet):
        if self.capture:
            self.capture.packet(packet, 0)
        self.client.request("usb_send", packet=bytes(packet).hex())


class NativeTransport:
    def __init__(self, client, spec, *, le_audio=False):
        self.client = client
        target = transport_target(spec)
        if le_audio:
            target["le_audio"] = True
        from .capture import ControlCapture
        self.capture = ControlCapture.configured()
        try:
            self.client.request("usb_open", **target)
        except BaseException:
            if self.capture:
                self.capture.close()
            raise
        self.source, self.sink = _Source(client, guard_commands=le_audio, capture=self.capture), _Sink(client, self.capture)

    async def __aenter__(self):
        return self

    async def wait_ready(self, report=None):
        """Bootstrap runs natively; this wait is cancellable and carries no HCI."""
        previous = None
        messages = {
            "opening": "正在打开蓝牙适配器…",
            "probing": "正在检查蓝牙控制器…",
            "validating": "正在校验蓝牙启动固件…",
            "downloading": "正在加载蓝牙启动固件…",
            "booting": "正在启动蓝牙控制器…",
            "configuring": "正在应用蓝牙配置…",
        }
        while not self.source.ready.is_set():
            state = self.source.startup.get("firmware_state", "opening")
            if report and state != previous and state in messages:
                report(messages[state])
            previous = state
            try:
                await asyncio.wait_for(self.source.ready.wait(), 0.1)
            except TimeoutError:
                continue
        if self.source.failure:
            raise self.source.failure
        if self.source.terminated.done():
            raise ConnectionError("蓝牙适配器在启动时断开，请检查连接和专用接收驱动")

    async def __aexit__(self, exc_type, _error, _trace):
        self.source.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.source.task
        if self.source.command_owner is not None:
            await self.source.command_owner.wait_closed()
        try:
            self.client.request("usb_close")
        except OSError:
            if exc_type is None:
                raise
        finally:
            if self.capture:
                result = await asyncio.to_thread(self.capture.close)
                logging.getLogger(__name__).info("HCI diagnostic capture closed: %s", result)
