"""Versioned JSON control over private inherited stdio pipes. Audio stays local."""
from dataclasses import asdict, fields
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import threading

from . import __version__
from .backend import Runner, Settings, usb_adapters
from .diagnostics import MetadataOnly
from .audio_runtime import on_audio_thread

PROTOCOL_VERSION = 1
MAX_COMMAND = 65536


def validate_gain(value):
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError("音量范围为 0%–200%")
    return float(value)


@on_audio_thread
def inventory():
    import sounddevice as sd
    apis = sd.query_hostapis()
    audio = []
    for i, endpoint in enumerate(sd.query_devices()):
        api = apis[endpoint["hostapi"]]["name"]
        if "WASAPI" in api:
            from .endpoints import portaudio_id
            audio.append(dict(endpoint, index=i, api=api, endpoint_id=portaudio_id(i)))
    return {"adapters": usb_adapters(), "audio": audio}


def validate_settings(values):
    valid = {field.name for field in fields(Settings)}
    if not isinstance(values, dict) or set(values) - valid:
        raise ValueError("设置格式不受支持")
    settings = Settings(**values)
    if settings.mode not in ("advanced", "native"):
        raise ValueError("未知接收方式")
    if settings.mode == "advanced" and not re.fullmatch(r"usb:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}#\d{1,2}(?:\+sco=0)?", settings.transport):
        raise ValueError("请选择有效蓝牙适配器")
    if settings.codec not in ("auto", "sbc", "aac", "aac_192", "aptx", "aptx_hd", "ldac"):
        raise ValueError("未知媒体编码")
    if settings.media_mode not in ("single", "mix"):
        raise ValueError("未知出声方式")
    if not isinstance(settings.name, str) or not settings.name.strip() or len(settings.name.encode("utf-8")) > 200:
        raise ValueError("耳机名称为空或过长")
    if type(settings.maximum_devices) is not int or not 1 <= settings.maximum_devices <= 4:
        raise ValueError("连接上限应为 1–4")
    if type(settings.buffer_ms) is not int or settings.buffer_ms not in (30, 40, 60, 90, 120):
        raise ValueError("不支持的媒体缓冲")
    if type(settings.call_buffer_ms) is not int or settings.call_buffer_ms not in (20, 30, 40, 60):
        raise ValueError("不支持的通话缓冲")
    for device in (settings.input_device, settings.output_device):
        if device is not None and (type(device) is not int or not 0 <= device < 4096):
            raise ValueError("无效声音设备")
    for identity in (settings.output_endpoint_id, settings.input_endpoint_id):
        if identity is not None and (not isinstance(identity, str) or not 1 <= len(identity) <= 512):
            raise ValueError("无效声音设备标识")
    for value in (settings.calls, settings.ldac_enabled, settings.microphone_muted, settings.system_audio_enabled):
        if type(value) is not bool:
            raise ValueError("开关值必须为布尔值")
    for value in (settings.playback_gain, settings.microphone_gain, settings.system_gain):
        validate_gain(value)
    if not isinstance(settings.device_volumes, dict) or len(settings.device_volumes) > 128:
        raise ValueError("设备音量设置无效")
    for key, gains in settings.device_volumes.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or not isinstance(gains, dict) or set(gains) - {"down", "up"}:
            raise ValueError("设备音量设置无效")
        for value in gains.values():
            validate_gain(value)
    if settings.codec == "ldac" and not settings.ldac_enabled:
        raise ValueError("请先启用实验性 LDAC")
    return settings


class Engine:
    def __init__(self, directory, output):
        self.directory = Path(directory)
        self.output = output
        self.write_lock = threading.Lock()
        self.settings_lock = threading.RLock()
        self.runner = Runner(self.emit)
        self.settings = Settings()
        self.closed = False
        self.inventory_lock = threading.Lock()
        self.audio_refresh_lock = threading.Lock()
        self.monitor_stop = threading.Event()
        path = self.directory / "settings.json"
        if path.exists():
            try:
                values = json.loads(path.read_text("utf-8"))
                self.settings = Settings(**{k: v for k, v in values.items() if k in {f.name for f in fields(Settings)}})
            except (OSError, ValueError, TypeError):
                logging.exception("Settings could not be loaded")

    def emit(self, event, **values):
        message = {"event": event, "protocol": PROTOCOL_VERSION, **values}
        with self.write_lock:
            try:
                self.output.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
                self.output.flush()
            except (BrokenPipeError, OSError):
                self.runner.stop()
        if event not in ("inventory", "telemetry", "gain", "master_gain"):
            logging.getLogger(__name__).info("%s %s", event, values)
        if event == "audio_device" and values.get("success"):
            setattr(self.settings, values["direction"] + "_device", values["device"])
            setattr(self.settings, values["direction"] + "_endpoint_id", values.get("endpoint_id"))
            try:
                self.save()
            except OSError:
                logging.exception("Endpoint choice could not be saved")
        if event == "media_mode" and values.get("success"):
            self.settings.media_mode = values["mode"]
            self.save()
        if event == "system_audio":
            self.settings.system_audio_enabled = values.get("enabled", False)
            self.save()

    def save(self):
        with self.settings_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / "settings.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(self.settings), ensure_ascii=False, indent=2), "utf-8")
            temporary.replace(path)

    def refresh(self):
        if not self.inventory_lock.acquire(blocking=False):
            return
        def scan():
            try:
                self.emit("inventory", **inventory())
            except Exception as exc:
                self.emit("error", message=f"设备检查失败：{exc}")
            finally:
                self.inventory_lock.release()
        threading.Thread(target=scan, daemon=True, name="inventory").start()

    def remap_saved_audio(self, settings):
        from .endpoints import find_endpoint
        for direction in ("output", "input"):
            identity = getattr(settings, direction + "_endpoint_id")
            if identity is not None:
                try:
                    setattr(settings, direction + "_device", find_endpoint(identity, direction))
                except ValueError:
                    setattr(settings, direction + "_device", None)
                    setattr(settings, direction + "_endpoint_id", None)

    def reconfigure_audio(self):
        if not self.audio_refresh_lock.acquire(blocking=False):
            return False
        try:
            from .audio_runtime import refresh_devices
            import asyncio
            receiver = self.runner.receiver
            running = self.runner.thread is not None and self.runner.thread.is_alive()
            if running and (receiver is None or (receiver.settings.mode != "native" and receiver.output is None)):
                return False
            self.emit("audio_refreshing")
            if running and receiver:
                mapped, errors = asyncio.run_coroutine_threadsafe(receiver.refresh_audio(), self.runner.loop).result(20)
            else:
                mapped, errors = refresh_devices({"output": self.settings.output_device, "input": self.settings.input_device})
            self.settings.output_device, self.settings.input_device = mapped["output"], mapped["input"]
            self.save()
            self.emit("audio_refreshed", success=not errors, output_device=mapped["output"], input_device=mapped["input"],
                      message="音频设备已更新，默认设备自动跟随系统" if not errors else "部分音频设备暂不可用，将自动重试")
            self.refresh()
            return not errors
        except Exception:
            logging.exception("Audio device refresh failed")
            self.emit("audio_refreshed", success=False, output_device=self.settings.output_device,
                      input_device=self.settings.input_device, message="音频设备更新未完成，将自动重试")
            return False
        finally:
            self.audio_refresh_lock.release()

    def start_monitor(self):
        def watch():
            from .endpoints import snapshot
            known = previous = None
            while not self.monitor_stop.wait(1):
                try:
                    state = snapshot()
                    if known is None:
                        known = state
                    from .audio_runtime import streams_unhealthy
                    if (state != known and state == previous) or streams_unhealthy():
                        if self.reconfigure_audio():
                            known = state
                    previous = state
                except Exception:
                    logging.debug("Core Audio monitor unavailable", exc_info=True)
        threading.Thread(target=watch, daemon=True, name="audio-device-watch").start()

    def command(self, message):
        if not isinstance(message, dict) or message.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("界面与后台版本不一致")
        action = message.get("command")
        running = self.runner.thread is not None and self.runner.thread.is_alive()
        if action == "hello":
            self.emit("hello", version=__version__, settings=asdict(self.settings), running=running)
        elif action == "inventory":
            self.refresh()
        elif action == "rescan_audio":
            threading.Thread(target=self.reconfigure_audio, daemon=True, name="audio-rescan").start()
        elif action in ("configure", "start"):
            # The stopped event is emitted during the old thread's final cleanup.
            # Finish joining it before accepting an intentional codec restart.
            stopping = getattr(self.runner, "stop_requested", None)
            if running and stopping is not None and stopping.is_set():
                self.runner.thread.join(timeout=5)
                running = self.runner.thread.is_alive()
            if running:
                raise ValueError("请先停止接收再更改连接设置")
            settings = validate_settings(message.get("settings"))
            with self.audio_refresh_lock:
                self.remap_saved_audio(settings)
                self.settings = settings
                self.save()
                if action == "start":
                    self.emit("starting")
                    self.runner.start(settings, self.directory)
                else:
                    self.emit("configured", settings=asdict(self.settings))
        elif action == "stop":
            self.runner.stop()
        elif action == "audio_device":
            direction, device = message.get("direction"), message.get("device")
            if direction not in ("input", "output") or (device is not None and (type(device) is not int or not 0 <= device < 4096)):
                raise ValueError("无效声音设备")
            request_id = message.get("request_id", 0)
            endpoint_id = message.get("endpoint_id")
            if endpoint_id is not None and (not isinstance(endpoint_id, str) or not 1 <= len(endpoint_id) <= 512):
                raise ValueError("无效声音设备标识")
            if running:
                if endpoint_id is None:
                    self.runner.set_audio_device(direction, device, request_id)
                else:
                    self.runner.set_audio_device(direction, device, request_id, endpoint_id)
            else:
                with self.audio_refresh_lock:
                    if endpoint_id is not None:
                        from .endpoints import find_endpoint
                        device = find_endpoint(endpoint_id, direction)
                    setattr(self.settings, direction + "_device", device)
                    setattr(self.settings, direction + "_endpoint_id", endpoint_id)
                    self.save()
                    self.emit("audio_device", direction=direction, device=device, request_id=request_id, success=True, endpoint_id=endpoint_id)
        elif action == "media_mode":
            mode = message.get("mode")
            if mode not in ("single", "mix"):
                raise ValueError("未知出声方式")
            if self.settings.mode == "native":
                raise ValueError("共用模式的媒体连接由 Windows 管理")
            if running:
                self.runner.action("media_mode", mode)
            else:
                self.emit("media_mode", success=True, mode=mode)
        elif action == "device_action":
            operation = message.get("action")
            if operation not in ("answer", "hangup", "call_audio", "select_call", "disconnect", "select_media"):
                raise ValueError("不支持的设备操作")
            self.runner.action(operation, str(message.get("key", ""))[:200])
        elif action == "gain":
            gain = validate_gain(message.get("gain", 1))
            key = str(message.get("key", ""))[:200]
            direction = message.get("direction", "down")
            if not key or direction not in ("down", "up"):
                raise ValueError("无效音量方向或设备")
            with self.settings_lock:
                if key not in self.settings.device_volumes and len(self.settings.device_volumes) >= 128:
                    raise ValueError("保存的设备音量数量已达上限")
                gains = self.settings.device_volumes.setdefault(key, {"down": 1.0, "up": 1.0})
                gains[direction] = gain
            if self.runner.receiver:
                self.runner.receiver.set_device_gain(key, direction, gain)
            self.save()
        elif action == "master_gain":
            bus = message.get("bus")
            if bus not in ("playback", "microphone", "system"):
                raise ValueError("无效总音量通道")
            gain = validate_gain(message.get("gain"))
            setattr(self.settings, bus + "_gain", gain)
            if self.runner.receiver:
                self.runner.receiver.set_master_gain(bus, gain)
            self.save()
        elif action == "system_audio":
            enabled = message.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("无效电脑声音开关")
            if enabled:
                from .loopback import supported
                if not supported():
                    self.emit("system_audio", success=False, enabled=False, request_id=message.get("request_id", 0),
                              message="电脑声音混合需要 Windows 11 或 build 20348 及以上")
                    return
            if running:
                self.runner.set_system_audio(enabled, message.get("request_id", 0))
            else:
                self.emit("system_audio", success=True, enabled=enabled, request_id=message.get("request_id", 0))
        elif action == "mute":
            if type(message.get("muted")) is not bool:
                raise ValueError("无效静音状态")
            self.settings.microphone_muted = message["muted"]
            if self.runner.receiver and self.runner.receiver.calls:
                self.runner.receiver.calls.router.muted = self.settings.microphone_muted
            self.save()
        elif action == "telemetry":
            receiver = self.runner.receiver
            self.emit("telemetry", running=running, audio=receiver.mixer.snapshot() if receiver else {},
                      devices=receiver.live_snapshot() if receiver else [])
        elif action == "shutdown":
            self.closed = True
            self.runner.stop()
        else:
            raise ValueError("不支持的后台命令")


def main(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "bluehead.log", maxBytes=750000, backupCount=2, encoding="utf-8")
    handler.addFilter(MetadataOnly())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("bluehead").setLevel(logging.INFO)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    engine = Engine(directory, sys.stdout)
    engine.remap_saved_audio(engine.settings)
    engine.emit("hello", version=__version__, settings=asdict(engine.settings), running=False)
    engine.start_monitor()
    try:
        while not engine.closed:
            line = sys.stdin.buffer.readline(MAX_COMMAND + 1)
            if not line:
                break
            if len(line) > MAX_COMMAND:
                raise ValueError("后台命令过长")
            try:
                engine.command(json.loads(line))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                engine.emit("error", message=str(exc))
    finally:
        engine.monitor_stop.set()
        engine.runner.stop()
        if engine.runner.thread:
            engine.runner.thread.join(8)
    return 0
