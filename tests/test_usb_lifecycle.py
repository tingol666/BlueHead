"""Faults in real C++ USB ownership, without loading a physical USB backend."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def usb_probe(tmp_path_factory):
    output = tmp_path_factory.mktemp('usb-faults')
    compiler = shutil.which('g++') or 'D:/Code/MSYS2/ucrt64/bin/g++.exe'
    if not Path(compiler).is_file():
        pytest.skip('MinGW native test compiler unavailable')
    include = ROOT / 'work/runtime-build'
    if not (include / 'libusb.h').is_file():
        pytest.skip('Run tools/build_runtime.py to prepare pinned USB headers')
    cc = str(Path(compiler).with_name('gcc.exe'))
    subprocess.run([cc, '-shared', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-Wl,--export-all-symbols', '-Wl,--no-insert-timestamp', '-I', str(include),
                    str(ROOT / 'tests/native_usb_fake.c'), '-o', str(output / 'libusb-1.0.dll')], check=True)
    executable = output / 'usb-lifecycle.exe'
    subprocess.run([compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-municode',
                    '-static', '-Wl,--no-insert-timestamp', '-I', str(include),
                    '-I', str(ROOT / 'vendor/nlohmann-json'), '-I', str(ROOT / 'native'),
                    str(ROOT / 'tests/native_usb_lifecycle.cpp'), '-o', str(executable),
                    '-lbcrypt', '-lavrt'], check=True)
    return executable


@pytest.mark.parametrize('fault', ['none', 'open-denied', 'submit-failed', 'control-timeout'])
def test_usb_startup_failure_is_explicit_and_retires_all_requests_before_release(usb_probe, fault):
    env = dict(os.environ, BLUEHEAD_USB_FAULT=fault)
    process = subprocess.run([str(usb_probe), str(usb_probe.parent)], capture_output=True,
                             text=True, encoding='utf-8', env=env, timeout=8,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report['invalid_releases'] == report['pending_after_close'] == 0, report
    if fault == 'open-denied':
        assert 'LIBUSB_ERROR_ACCESS (-3)' in report['error']
    elif fault == 'submit-failed':
        assert 'initial HCI USB reads' in report['error']
    else:
        assert 'error' not in report, report
        assert report['opened'] and report['closed'] and report['reopened']
        if fault == 'control-timeout':
            assert report['terminal']['last_transfer_status'] == 2
            assert report['seconds'] < 3
