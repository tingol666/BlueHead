import tkinter as tk
import pytest

from bluehead.app import App


@pytest.fixture(scope="module")
def desktop():
    # Tk supports multiple windows under one interpreter. Repeated Tcl teardown
    # in the same Windows process is unreliable in this Python/Tk distribution.
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


def test_desktop_ui_and_event_updates(tmp_path, desktop):
    root = tk.Toplevel(desktop)
    root.withdraw()
    try:
        app = App(root, tmp_path, scan=False)
        app.emit("device", key="tablet", state="已连接")
        app.emit("format", key="tablet", codec="aac", rate=48000, channels=2)
        app.drain()
        assert "AAC" in app.table.item("tablet", "values")[1]
        app.mode.set(app.MODES[1])
        app.mode_changed()
        assert "通话" in app.mode_hint.get()
        assert str(app.adapter_picker.cget("state")) == "disabled"
    finally:
        root.destroy()


def test_live_endpoint_choices_remain_enabled_and_failed_switch_restores_selection(tmp_path, desktop):
    from types import SimpleNamespace
    root = tk.Toplevel(desktop)
    root.withdraw()
    try:
        app = App(root, tmp_path, scan=False)
        app.audio = [{"index": 3, "name": "Speaker A"}, {"index": 7, "name": "Speaker B"}]
        app.settings.output_device = 3
        app.output.set("3 · Speaker A")
        requested = []
        app.runner = SimpleNamespace(set_audio_device=lambda *args: requested.append(args))
        app.set_running(True)
        assert str(app.output_picker.cget("state")) == "readonly"
        assert str(app.input_picker.cget("state")) == "readonly"
        assert str(app.adapter_picker.cget("state")) == "disabled"
        app.output.set("7 · Speaker B")
        app.audio_selected("output")
        assert requested[0][:2] == ("output", 7)
        app.emit("audio_device", direction="output", request_id=requested[0][2], device=7,
                 success=False, message="unavailable")
        app.drain()
        assert app.output.get() == "3 · Speaker A" and app.settings.output_device == 3
        assert str(app.output_picker.cget("state")) == "readonly"
    finally:
        root.destroy()
