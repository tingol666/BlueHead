"""Versioned JSON control over private inherited stdio pipes. Audio stays local."""
from dataclasses import asdict, fields
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading

from . import __version__
from .backend import Runner, Settings, usb_adapters
from .diagnostics import MetadataOnly
from .audio_runtime import on_audio_thread
from .quality_log import AudioCounters
from .errors import AudioHealth
from .controller import transport_target

PROTOCOL_VERSION = 1
MAX_COMMAND = 65536


def validate_gain(value):
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError("音量范围为 0%–200%")
    return float(value)


@on_audio_thread
def inventory():
    from .endpoints import audio_inventory
    return {"adapters": usb_adapters(), "audio": audio_inventory()}


def validate_settings(values, *, require_transport=True):
    valid = {field.name for field in fields(Settings)}
    if not isinstance(values, dict) or set(values) - valid:
        raise ValueError("设置格式不受支持")
    settings = Settings(**values)
    if settings.mode not in ("advanced", "native"):
        raise ValueError("未知接收方式")
    if settings.mode == "advanced" and (require_transport or settings.transport != ""):
        transport_target(settings.transport)
    if settings.codec not in ("auto", "sbc", "aac", "aac_192", "aptx", "aptx_hd", "ldac", "lhdc5"):
        raise ValueError("未知媒体编码")
    if settings.connection_policy not in ('prefer_le', 'classic'):
        raise ValueError('请选择优先 LE Audio / LC3 或仅经典蓝牙')
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
    for device in (settings.input_device, settings.output_device, settings.system_device):
        if device is not None and (type(device) is not int or not 0 <= device < 4096):
            raise ValueError("无效声音设备")
    for identity in (settings.output_endpoint_id, settings.input_endpoint_id, settings.system_endpoint_id):
        if identity is not None and (not isinstance(identity, str) or not 1 <= len(identity) <= 512):
            raise ValueError("无效声音设备标识")
    for value in (settings.calls, settings.voice_single_device, settings.ldac_enabled, settings.lhdc5_enabled, settings.le_audio_enabled, settings.microphone_muted, settings.system_audio_enabled):
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
    from .codec_policy import canonical, permitted
    allowed = permitted(settings)
    if settings.codec != 'auto' and canonical(settings.codec) not in allowed:
        raise ValueError('首选编码必须先在“允许的编码”中启用')
    settings.ldac_enabled = 'ldac' in allowed
    settings.lhdc5_enabled = 'lhdc5' in allowed
    return settings


class Engine:
    def __init__(self, directory, output):
        self.directory = Path(directory)
        self.output = output
        self.audio_counters = AudioCounters(self.directory)
        self.audio_health = AudioHealth()
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
        # Transport metadata must never replace the IPC envelope/version.
        message = {**values, "event": event, "protocol": PROTOCOL_VERSION}
        with self.write_lock:
            try:
                self.output.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
                self.output.flush()
            except (BrokenPipeError, OSError):
                self.runner.stop()
        if event not in ("inventory", "telemetry", "gain", "master_gain", "avrcp", "heartbeat"):
            logging.getLogger(__name__).info("%s %s", event, values)
        if event == 'telemetry':
            self.audio_counters.record(values.get('audio', {}))
            for notice in self.audio_health.events(values.get('audio', {})):
                self.emit('audio_health', **notice)
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
        for direction in ("output", "input", "system"):
            identity = getattr(settings, direction + "_endpoint_id")
            if identity is not None:
                try:
                    setattr(settings, direction + "_device", find_endpoint(identity, "output" if direction == "system" else direction))
                except ValueError:
                    setattr(settings, direction + "_device", None)
                    if settings.mode != 'advanced' and direction != 'system':
                        setattr(settings, direction + "_endpoint_id", None)

    def reconfigure_audio(self, force=False):
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
                operation = receiver.refresh_audio(force=True) if force else receiver.refresh_audio()
                mapped, errors = asyncio.run_coroutine_threadsafe(operation, self.runner.loop).result(20)
            else:
                mapped, errors = refresh_devices({"output": self.settings.output_device, "input": self.settings.input_device}, {
                    "output": self.settings.output_endpoint_id, "input": self.settings.input_endpoint_id})
            self.settings.output_device, self.settings.input_device = mapped["output"], mapped["input"]
            self.save()
            self.emit("audio_refreshed", success=not errors, output_device=mapped["output"], input_device=mapped["input"],
                      output_endpoint_id=self.settings.output_endpoint_id, input_endpoint_id=self.settings.input_endpoint_id,
                      message="音频设备已更新，默认设备自动跟随系统" if not errors else str(errors[0]))
            self.refresh()
            return not errors
        except Exception:
            logging.exception("Audio device refresh failed")
            self.emit("audio_refreshed", success=False, output_device=self.settings.output_device,
                      input_device=self.settings.input_device,
                      output_endpoint_id=self.settings.output_endpoint_id, input_endpoint_id=self.settings.input_endpoint_id,
                      message="音频设备更新未完成，请稍后重试")
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
        elif action == "heartbeat":
            self.emit("heartbeat")
        elif action == "inventory":
            self.refresh()
        elif action == "rescan_audio":
            threading.Thread(target=self.reconfigure_audio, kwargs={'force': True}, daemon=True, name="audio-rescan").start()
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
            # Also acknowledge a stop racing the receiver's final stopped event.
            if not running:
                self.emit("stopped")
        elif action == "audio_device":
            direction, device = message.get("direction"), message.get("device")
            if direction not in ("input", "output", "system") or (device is not None and (type(device) is not int or not 0 <= device < 4096)):
                raise ValueError("无效声音设备")
            request_id = message.get("request_id", 0)
            endpoint_id = message.get("endpoint_id")
            if endpoint_id is not None and (not isinstance(endpoint_id, str) or not 1 <= len(endpoint_id) <= 512):
                raise ValueError("无效声音设备标识")
            if direction == "system" and device is not None and endpoint_id is None:
                raise ValueError("请选择具体电脑声音来源，或使用全部应用声音")
            if running:
                if endpoint_id is None:
                    self.runner.set_audio_device(direction, device, request_id)
                else:
                    self.runner.set_audio_device(direction, device, request_id, endpoint_id)
            else:
                with self.audio_refresh_lock:
                    if endpoint_id is not None:
                        from .endpoints import find_endpoint
                        device = find_endpoint(endpoint_id, "output" if direction == "system" else direction)
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
            if operation not in ("answer", "hangup", "call_audio", "select_call", "disconnect", "select_media",
                                  "play", "pause", "play_pause", "stop", "next", "previous"):
                raise ValueError("不支持的设备操作")
            self.runner.action(operation, str(message.get("key", ""))[:200])
        elif action in ('codec_options', 'codec_switch'):
            from .codec_sessions import validate
            values = validate(message, switch=action == 'codec_switch')
            if not running or self.settings.mode == 'native':
                self.emit(action, key=values['key'], request_id=values['request_id'],
                          success=False, done=True, phase='failed',
                          message='请先使用 BlueHead 专用蓝牙开始接收，并在这台设备上播放媒体。')
            else:
                self.runner.action(action, values)
        elif action == 'preferences':
            patch = message.get('values')
            allowed = {'codec', 'allowed_codecs', 'ldac_enabled', 'lhdc5_enabled', 'connection_policy',
                       'le_audio_enabled', 'name', 'maximum_devices', 'buffer_ms', 'call_buffer_ms',
                       'calls', 'voice_single_device'}
            if not isinstance(patch, dict) or set(patch) - allowed:
                raise ValueError('自动保存设置格式无效')
            with self.settings_lock:
                values = {**asdict(self.settings), **patch}
                next_settings = validate_settings(values, require_transport=False)
                previous_settings = self.settings
                self.settings = next_settings
                try:
                    self.save()
                except OSError:
                    self.settings = previous_settings
                    self.emit('preferences', success=False, request_id=message.get('request_id', 0),
                              message='设置未能保存，请检查数据目录是否可写，再修改一次或重新开始接收。')
                    return
            if running:
                live = {name: getattr(next_settings, name) for name in (
                    'codec', 'allowed_codecs', 'ldac_enabled', 'lhdc5_enabled')}
                self.runner.action('codec_preferences', live)
            self.emit('preferences', success=True, settings=asdict(next_settings),
                      request_id=message.get('request_id', 0), deferred=running,
                      message='已自动保存；连接设置在下次开始接收时生效。' if running else '已自动保存。')
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
            if self.runner.receiver:
                self.runner.receiver.set_microphone_muted(self.settings.microphone_muted)
            self.save()
        elif action == 'broadcast':
            from .le_audio.commands import validate
            values = validate(message)
            if not running or self.settings.mode == 'native':
                raise ValueError('请先在音频中心使用进阶模式，点击“开始接收”，再搜索音频广播。')
            self.runner.action('broadcast', values)
        elif action == "telemetry":
            if not running or not self.runner.telemetry(self.settings.mode != "native"):
                self.emit("telemetry", running=False, audio={}, devices=[],
                          devices_complete=self.settings.mode != "native", broadcast=None)
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
