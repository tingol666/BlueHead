"""Controller liveness through the existing Bumble command/credit owner only."""

import asyncio
import time
from bumble import hci


class ControllerUnresponsive(ConnectionError):
    category = "controller_unresponsive"
    firmware_profile = ""


class ControllerInitializationTimeout(TimeoutError):
    category = "controller_unresponsive"
    firmware_profile = ""


class ControllerConnectionLost(ControllerUnresponsive):
    category = "controller_connection_lost"


class ControllerChannelLost(ConnectionError):
    category = "native_channel"
    firmware_profile = ""


async def _command(device, command, stop_event, timeout, *, check_result=True):
    pending = asyncio.create_task(device.send_command(command, check_result=check_result))
    remaining = timeout
    quantum = min(0.05, timeout / 4)
    previous = time.monotonic()
    try:
        while not pending.done():
            if stop_event.is_set():
                return None
            budget = min(quantum, remaining)
            await asyncio.wait({pending}, timeout=budget)
            now = time.monotonic()
            elapsed = now - previous
            previous = now
            # Only charge the requested wait interval. Timer overshoot includes
            # Python blocking and Windows timer granularity, neither of which
            # proves that the controller failed to respond.
            remaining -= min(elapsed, budget)
            if stop_event.is_set():
                return None
            if not pending.done() and remaining <= 0:
                raise TimeoutError("Controller response deadline")
        return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def watch_controller(device, stop_event, *, interval=3.0, timeout=3.0, connections=None):
    """No audio-idle heuristic: paused media is healthy. Do not send raw HCI or
    reset the controller while a session owns it. A deadline failure retires the
    entire old session before any recovery can acquire the same adapter.
    """
    missing = {}
    turn = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), interval)
            return
        except TimeoutError:
            pass
        if stop_event.is_set():
            return
        try:
            await _command(device, hci.HCI_Read_Local_Version_Information_Command(), stop_event, timeout)
            targets = list(connections()) if connections else []
            live = {(id(peer), peer.handle) for peer in targets if isinstance(getattr(peer, "handle", None), int)}
            missing = {key: count for key, count in missing.items() if key in live}
            host = getattr(device, "host", None)
            supports = getattr(host, "supports_command", lambda _opcode: True)
            if targets and supports(hci.HCI_READ_RSSI_COMMAND) and not stop_event.is_set():
                peer = targets[turn % len(targets)]
                turn += 1
                handle = getattr(peer, "handle", None)
                if not isinstance(handle, int):
                    continue
                response = await _command(
                    device, hci.HCI_Read_RSSI_Command(handle=handle), stop_event, timeout, check_result=False
                )
                current = list(connections())
                if not any(candidate is peer and candidate.handle == handle for candidate in current):
                    continue
                status = getattr(getattr(response, "return_parameters", response), "status", 0)
                key = (id(peer), handle)
                if status == hci.HCI_UNKNOWN_CONNECTION_IDENTIFIER_ERROR:
                    missing[key] = missing.get(key, 0) + 1
                    if missing[key] >= 2:
                        raise ControllerConnectionLost(
                            "控制器已失去连接状态且未报告断连，旧连接已清除。请打开“蓝牙设置”，点击“恢复蓝牙”。"
                        )
                else:
                    missing.pop(key, None)
        except asyncio.CancelledError:
            raise
        except ControllerConnectionLost:
            raise
        except (hci.HCI_Error, hci.HCI_StatusError):
            # An explicit rejection is a reply, not a liveness timeout.
            continue
        except TimeoutError as error:
            if stop_event.is_set():
                return
            raise ControllerUnresponsive(
                "蓝牙控制器已停止响应，连接已清除。请打开“蓝牙设置”，点击“恢复蓝牙”。"
            ) from error
        except Exception as error:
            if stop_event.is_set():
                return
            raise ControllerChannelLost("蓝牙后台通信已中断，请重新开始接收。") from error
