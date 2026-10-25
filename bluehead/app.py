"""Local desktop controls; no cloud account, WLAN transport, or auto driver changes."""
from dataclasses import asdict, fields
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from . import __version__
from .backend import Settings, Runner, usb_adapters

log = logging.getLogger(__name__)


def app_directory():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


class App:
    MODES = ("高音质与通话 · 独占适配器", "原生媒体 · 共用现有蓝牙")
    CODECS = {"自动协商": "auto", "AAC": "aac", "aptX": "aptx", "aptX HD": "aptx_hd", "SBC": "sbc", "LDAC（实验）": "ldac"}

    def __init__(self, root, directory=None, scan=True):
        self.root = root
        self.directory = directory or app_directory() / "data"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.runner = Runner(self.emit)
        self.running = False
        self.audio_requests = {}
        self.audio_request_id = 0
        self.audio_inventory_loaded = False
        self.devices, self.adapters, self.audio = {}, [], []
        self.settings = Settings()
        self.settings_path = self.directory / "settings.json"
        if self.settings_path.exists():
            try:
                data = json.loads(self.settings_path.read_text("utf-8"))
                valid = {f.name for f in fields(Settings)}
                self.settings = Settings(**{k: v for k, v in data.items() if k in valid})
            except (ValueError, TypeError, OSError):
                pass
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self.drain)
        if scan:
            self.refresh()

    def emit(self, kind, **values):
        if kind != "inventory":
            log.info("%s %s", kind, values)
        self.events.put((kind, values))

    def build(self):
        self.root.title(f"BlueHead · {__version__}")
        self.root.geometry("1060x790")
        self.root.minsize(980, 650)
        self.root.configure(bg="#101720")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#101720")
        style.configure("TLabel", background="#101720", foreground="#e9f0f5", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"), foreground="#76d9cc")
        style.configure("Muted.TLabel", foreground="#a5b8c7")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("TCheckbutton", background="#101720", foreground="#e9f0f5", font=("Microsoft YaHei UI", 10))
        style.configure("TLabelframe", background="#101720", bordercolor="#334352")
        style.configure("TLabelframe.Label", background="#101720", foreground="#a5b8c7", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=33, font=("Microsoft YaHei UI", 10))
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, background="#101720", highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        page = ttk.Frame(canvas, padding=24)
        window = canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        ttk.Label(page, text="BlueHead", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="本机蓝牙音频中心  /  多设备媒体接收与通话", style="Muted.TLabel").pack(anchor="w", pady=(0, 17))
        controls = ttk.LabelFrame(page, text="连接", padding=14)
        controls.pack(fill="x")
        controls.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(value=self.MODES[self.settings.mode == "native"])
        self.adapter = tk.StringVar()
        self.name = tk.StringVar(value=self.settings.name)
        self.codec = tk.StringVar(value=next((k for k, v in self.CODECS.items() if v == self.settings.codec), "自动协商"))
        self.ldac = tk.BooleanVar(value=self.settings.ldac_enabled)
        self.calls = tk.BooleanVar(value=self.settings.calls)
        self.buffer = tk.StringVar(value=str(self.settings.buffer_ms))
        self.max_devices = tk.StringVar(value=str(self.settings.maximum_devices))
        ttk.Label(controls, text="接收方式").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.mode_picker = ttk.Combobox(controls, textvariable=self.mode, values=self.MODES, state="readonly", width=36)
        self.mode_picker.grid(row=0, column=1, sticky="ew")
        self.mode_picker.bind("<<ComboboxSelected>>", self.mode_changed)
        ttk.Button(controls, text="刷新设备", command=self.refresh).grid(row=0, column=2, padx=(12, 0))
        ttk.Label(controls, text="蓝牙适配器").grid(row=1, column=0, sticky="w", pady=9)
        self.adapter_picker = ttk.Combobox(controls, textvariable=self.adapter, state="readonly")
        self.adapter_picker.grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(controls, text="耳机名称").grid(row=2, column=0, sticky="w")
        self.name_entry = ttk.Entry(controls, textvariable=self.name)
        self.name_entry.grid(row=2, column=1, columnspan=2, sticky="ew")
        options = ttk.Frame(controls)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(options, text="媒体编码").pack(side="left")
        self.codec_picker = ttk.Combobox(options, textvariable=self.codec, values=list(self.CODECS), state="readonly", width=16)
        self.codec_picker.pack(side="left", padx=8)
        self.calls_switch = ttk.Checkbutton(options, text="允许通话", variable=self.calls)
        self.calls_switch.pack(side="left", padx=10)
        self.ldac_switch = ttk.Checkbutton(options, text="启用 LDAC 实验解码", variable=self.ldac)
        self.ldac_switch.pack(side="left", padx=10)
        ttk.Label(options, text="连接上限").pack(side="left", padx=(12, 6))
        self.maximum_picker = ttk.Spinbox(options, from_=1, to=4, width=3, textvariable=self.max_devices)
        self.maximum_picker.pack(side="left")
        self.mode_hint = tk.StringVar()
        ttk.Label(controls, textvariable=self.mode_hint, style="Muted.TLabel", wraplength=940).grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))

        sound = ttk.LabelFrame(page, text="本机声音", padding=14)
        sound.pack(fill="x", pady=12)
        sound.columnconfigure(1, weight=1)
        self.output = tk.StringVar(value="系统默认输出")
        self.input = tk.StringVar(value="系统默认麦克风")
        ttk.Label(sound, text="播放到").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.output_picker = ttk.Combobox(sound, textvariable=self.output, state="readonly")
        self.output_picker.grid(row=0, column=1, sticky="ew")
        self.output_picker.bind("<<ComboboxSelected>>", lambda _: self.audio_selected("output"))
        ttk.Label(sound, text="通话麦克风").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.input_picker = ttk.Combobox(sound, textvariable=self.input, state="readonly")
        self.input_picker.grid(row=1, column=1, sticky="ew", pady=(9, 0))
        self.input_picker.bind("<<ComboboxSelected>>", lambda _: self.audio_selected("input"))
        ttk.Label(sound, text="缓冲（毫秒）").grid(row=0, column=2, padx=(18, 6))
        self.buffer_picker = ttk.Combobox(sound, textvariable=self.buffer, values=(40, 60, 90, 120), state="readonly", width=5)
        self.buffer_picker.grid(row=0, column=3)
        self.mic_mute = tk.BooleanVar(value=self.settings.microphone_muted)
        ttk.Checkbutton(sound, text="麦克风静音", variable=self.mic_mute, command=self.set_mic).grid(row=1, column=2, columnspan=2, padx=(18, 0))

        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(row, text="开始接收", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(row, text="停止", command=self.runner.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(row, text="查看使用说明", command=self.open_help).pack(side="right")
        self.status = tk.StringVar(value="尚未启动 · 驱动和 Windows 蓝牙设置保持原样")
        ttk.Label(row, textvariable=self.status, style="Muted.TLabel", wraplength=650).pack(side="left", padx=8)

        self.table = ttk.Treeview(page, columns=("device", "media", "state", "call"), show="headings", height=5)
        for key, title, width in [("device", "移动设备", 220), ("media", "实际媒体格式", 220), ("state", "连接", 130), ("call", "通话 / 麦克风", 310)]:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=100)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.selection_changed)
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(10, 0))
        self.call_buttons = []
        for label, action in [("选为通话设备", "select_call"), ("接听", "answer"), ("挂断", "hangup"), ("连接通话音频", "call_audio"), ("断开设备", "disconnect")]:
            button = ttk.Button(actions, text=label, command=lambda action=action: self.device_action(action))
            button.pack(side="left", padx=(0, 7))
            self.call_buttons.append(button)
        ttk.Label(actions, text="音量").pack(side="left", padx=(12, 0))
        self.gain = tk.DoubleVar(value=100)
        ttk.Scale(actions, from_=0, to=100, variable=self.gain, command=self.set_gain).pack(side="left", fill="x", expand=True)
        ttk.Label(page, text="预览版：媒体使用 A2DP；通话使用 HFP。真实编码在连接后显示，蓝牙版本不代表编码或并发能力。", style="Muted.TLabel", wraplength=980).pack(anchor="w", pady=(12, 0))
        self.mode_changed()

    def mode_changed(self, event=None):
        native = self.mode.get() == self.MODES[1]
        self.adapter_picker.configure(state="disabled" if native else "readonly")
        for button in self.call_buttons:
            button.configure(state="disabled" if native else "normal")
        self.mode_hint.set(
            "原生模式：先在 Windows 中配对设备；与其他蓝牙设备共用。编码由 Windows 决定，通话和每设备音量不可用，播放到系统默认输出。"
            if native else
            "高音质模式：需要所选适配器已由 WinUSB 提供接口。会独占它，支持多路混音与通话；程序不会自动替换任何驱动。"
        )
        self.set_audio_picker_states()

    def set_audio_picker_states(self):
        native = self.mode.get() == self.MODES[1]
        for direction, picker in (("output", self.output_picker), ("input", self.input_picker)):
            picker.configure(state="disabled" if native or direction in self.audio_requests else "readonly")

    @staticmethod
    def audio_id(value):
        return int(value.split(" · ", 1)[0]) if " · " in value else None

    def audio_label(self, direction, device):
        default = "系统默认输出" if direction == "output" else "系统默认麦克风"
        if device is None:
            return default
        found = next((d for d in self.audio if d["index"] == device), None)
        return f"{device} · {found['name']}" if found else default

    def save_settings(self):
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self.settings), ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.settings_path)

    def audio_selected(self, direction):
        variable = self.output if direction == "output" else self.input
        device = self.audio_id(variable.get())
        if not self.running:
            setattr(self.settings, direction + "_device", device)
            try:
                self.save_settings()
            except OSError as exc:
                self.status.set(f"设置暂未保存：{exc}")
            return
        self.audio_request_id += 1
        self.audio_requests[direction] = self.audio_request_id
        self.set_audio_picker_states()
        self.status.set("正在切换播放设备…" if direction == "output" else "正在切换通话麦克风…")
        self.runner.set_audio_device(direction, device, self.audio_request_id)

    def refresh(self):
        def run():
            try:
                adapters = usb_adapters()
                import sounddevice as sd
                devices = [dict(d, index=i) for i, d in enumerate(sd.query_devices())]
                self.emit("inventory", adapters=adapters, audio=devices)
            except Exception as exc:
                self.emit("error", message=f"设备检查失败：{exc}")
        threading.Thread(target=run, daemon=True).start()

    def start(self):
        try:
            transport = next((a["transport"] for a in self.adapters if a["name"] == self.adapter.get()), "")
            native = self.mode.get() == self.MODES[1]
            if not native and not transport:
                raise ValueError("没有可选择的 USB 蓝牙适配器。可先选择原生模式，或查看使用说明。")
            if self.CODECS[self.codec.get()] == "ldac" and not self.ldac.get():
                raise ValueError("LDAC 解码仍为实验功能，需要勾选后使用。")
            name = self.name.get().strip()
            if not name or len(name.encode("utf-8")) > 200:
                raise ValueError("耳机名称不能为空或过长")
            maximum = int(self.max_devices.get())
            if not 1 <= maximum <= 4:
                raise ValueError("连接上限应为 1–4")
            settings = Settings(
                mode="native" if native else "advanced", transport=transport,
                codec=self.CODECS[self.codec.get()], name=name,
                buffer_ms=int(self.buffer.get()), maximum_devices=maximum,
                output_device=self.audio_id(self.output.get()), input_device=self.audio_id(self.input.get()),
                calls=self.calls.get(), ldac_enabled=self.ldac.get(),
                microphone_muted=self.mic_mute.get(),
            )
            self.settings = settings
            self.save_settings()
            self.set_running(True)
            self.running = True
            self.status.set("正在启动…")
            self.runner.start(settings, self.directory)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法启动", str(exc), parent=self.root)

    def drain(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "inventory":
                    self.adapters, self.audio = data["adapters"], data["audio"]
                    names = [a["name"] for a in self.adapters]
                    self.adapter_picker.configure(values=names)
                    if names and self.adapter.get() not in names:
                        self.adapter.set(next((a["name"] for a in self.adapters if a["transport"] == self.settings.transport), names[0]))
                    self.output_picker.configure(values=["系统默认输出"] + [f"{d['index']} · {d['name']}" for d in self.audio if d["max_output_channels"] >= 2])
                    self.input_picker.configure(values=["系统默认麦克风"] + [f"{d['index']} · {d['name']}" for d in self.audio if d["max_input_channels"] >= 1])
                    if not self.audio_inventory_loaded:
                        self.output.set(self.audio_label("output", self.settings.output_device))
                        self.input.set(self.audio_label("input", self.settings.input_device))
                        self.audio_inventory_loaded = True
                elif kind == "audio_device":
                    direction = data["direction"]
                    if self.audio_requests.get(direction) != data["request_id"]:
                        continue
                    self.audio_requests.pop(direction)
                    variable = self.output if direction == "output" else self.input
                    if data["success"]:
                        setattr(self.settings, direction + "_device", data["device"])
                        self.status.set("播放设备已切换" if direction == "output" else "通话麦克风已切换")
                        try:
                            self.save_settings()
                        except OSError as exc:
                            self.status.set(f"设备已切换，设置暂未保存：{exc}")
                    else:
                        variable.set(self.audio_label(direction, getattr(self.settings, direction + "_device")))
                        self.status.set(data["message"])
                    self.set_audio_picker_states()
                elif kind in ("status", "warning", "error"):
                    self.status.set(data.get("message", ""))
                elif kind == "stopped":
                    self.set_running(False)
                elif "key" in data:
                    key = data["key"]
                    row = self.devices.setdefault(key, [key, "等待协商", "已连接", "未连接"])
                    if kind == "device":
                        row[2] = data["state"]
                    elif kind == "name":
                        row[0] = data["name"]
                    elif kind == "format":
                        row[1] = f"{data['codec'].upper()} · {data['rate']} Hz · {data['channels']} 声道"
                    elif kind == "call":
                        row[3] = data["state"]
                    elif kind == "microphone":
                        row[3] = "麦克风使用中" if data["active"] else "麦克风已关闭"
                    if self.table.exists(key):
                        self.table.item(key, values=row)
                    else:
                        self.table.insert("", "end", iid=key, values=row)
        except queue.Empty:
            pass
        self.root.after(80, self.drain)

    def set_running(self, running):
        self.running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        for widget in (self.mode_picker, self.adapter_picker, self.codec_picker, self.buffer_picker):
            widget.configure(state="disabled" if running else "readonly")
        for widget in (self.name_entry, self.maximum_picker, self.calls_switch, self.ldac_switch):
            widget.configure(state="disabled" if running else "normal")
        if not running:
            self.audio_requests.clear()
            self.mode_changed()
        self.set_audio_picker_states()

    def device_action(self, action):
        selected = self.table.selection()
        if selected:
            self.runner.action(action, selected[0])

    def set_gain(self, value):
        selected = self.table.selection()
        if selected and self.runner.receiver:
            self.runner.receiver.mixer.set_gain(selected[0], float(value) / 100)

    def selection_changed(self, event=None):
        selected = self.table.selection()
        if selected and self.runner.receiver:
            stream = self.runner.receiver.mixer.inputs.get(selected[0])
            if stream:
                self.gain.set(stream.gain * 100)

    def set_mic(self):
        if self.runner.receiver and self.runner.receiver.calls:
            self.runner.receiver.calls.router.muted = self.mic_mute.get()

    def open_help(self):
        path = app_directory() / "使用说明.txt"
        if path.exists():
            os.startfile(path)
        else:
            messagebox.showinfo("使用说明", "高音质模式需要独占 WinUSB 蓝牙适配器。原生模式用于共享蓝牙媒体接收。\n配对和通话需要移动设备支持，真机验证前请按预览版使用。", parent=self.root)

    def close(self):
        self.runner.stop()
        if self.runner.thread and self.runner.thread.is_alive():
            self.status.set("正在关闭蓝牙与麦克风…")
            self.root.after(100, self._finish_close)
        else:
            self.root.destroy()

    def _finish_close(self, attempts=0):
        if self.runner.thread and self.runner.thread.is_alive() and attempts < 60:
            self.root.after(100, lambda: self._finish_close(attempts + 1))
        else:
            self.root.destroy()


def main():
    directory = app_directory() / "data"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "bluehead.log", maxBytes=500000, backupCount=2, encoding="utf-8")
    from .diagnostics import MetadataOnly
    handler.addFilter(MetadataOnly())
    logging.basicConfig(level=logging.DEBUG, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("bluehead").setLevel(logging.INFO)
    root = tk.Tk()
    App(root, directory)
    root.mainloop()
