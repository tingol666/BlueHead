from dataclasses import asdict
from io import StringIO
import json
from types import SimpleNamespace

import pytest

from bluehead.backend import Settings
from bluehead.engine import Engine, validate_settings


def command(name, **values):
    return {"protocol": 1, "command": name, **values}


def test_worker_control_keeps_audio_switch_transaction_and_private_state(tmp_path):
    output = StringIO()
    engine = Engine(tmp_path, output)
    engine.command(command("hello"))
    assert json.loads(output.getvalue())['protocol'] == 1
    requested = []
    engine.runner = SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True),
                                    set_audio_device=lambda *args: requested.append(args))
    engine.command(command("audio_device", direction="input", device=7, request_id=99))
    assert requested == [("input", 7, 99)]
    assert engine.settings.input_device is None
    engine.emit("audio_device", direction="input", device=7, request_id=99, success=False)
    assert engine.settings.input_device is None
    engine.emit("audio_device", direction="input", device=7, request_id=99, success=True)
    assert json.loads((tmp_path / 'settings.json').read_text('utf-8'))['input_device'] == 7
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize("field,value", [
    ('transport', 'usb:8087:0033#0;whoami'), ('maximum_devices', 999),
    ('buffer_ms', -1), ('call_buffer_ms', 0), ('codec', 'made-up'),
    ('output_device', True), ('name', ''), ('calls', 'true'),
])
def test_untrusted_worker_settings_are_rejected(field, value):
    values = asdict(Settings(transport='usb:8087:0033#0'))
    values[field] = value
    with pytest.raises(ValueError):
        validate_settings(values)


def test_worker_rejects_version_mismatch_and_reconfigure_during_call(tmp_path):
    engine = Engine(tmp_path, StringIO())
    with pytest.raises(ValueError):
        engine.command({'protocol': 2, 'command': 'start'})
    engine.runner = SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True))
    with pytest.raises(ValueError, match='停止'):
        engine.command(command('configure', settings=asdict(Settings())))
    assert not (tmp_path / 'settings.json').exists()
