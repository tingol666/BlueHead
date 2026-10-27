"""Bootloader, warm restart, corrupt assets, packet fragmentation and cancellation.

These run the actual C++ USB owner and signed image plan with an offline USB
model. No physical adapter, original-driver backup or system state is touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from tools.native_build import compiler, includes
from tools.firmware_assets import verify

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def intel_probe(tmp_path_factory):
    output = tmp_path_factory.mktemp('intel-bootstrap')
    header = ROOT / 'work/runtime-build'
    subprocess.run([str(Path(compiler(cxx=True)).with_name('gcc.exe')), '-shared', '-std=c11', '-O2',
                    '-Wall', '-Wextra', '-Werror', '-Wl,--export-all-symbols', '-I', str(header),
                    str(ROOT / 'tests/native_usb_fake.c'), '-o', str(output / 'libusb-1.0.dll')], check=True)
    executable = output / 'intel-bootstrap.exe'
    subprocess.run([compiler(cxx=True), '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-municode',
                    '-static', '-Wl,--no-insert-timestamp', *includes(header, ROOT / 'vendor/nlohmann-json'),
                    str(ROOT / 'tests/native_intel_bootstrap.cpp'), '-o', str(executable), '-lbcrypt', '-lavrt'], check=True)
    return executable


@pytest.mark.parametrize('fault', ['cold', 'warm', 'fragmented', 'boot-metadata', 'cancel', 'unsupported', 'ambiguous',
                                  'command-reject', 'signature-reject', 'ddc-reject',
                                  'command-timeout', 'boot-timeout', 'missing-image', 'corrupt-image'])
def test_bootstrap_owns_hci_until_firmware_ready_and_retires_every_transfer(intel_probe, tmp_path, fault):
    shutil.copy2(intel_probe.parent / 'libusb-1.0.dll', tmp_path)
    shutil.copytree(ROOT / 'bluehead/assets/intel', tmp_path / 'intel')
    if fault == 'missing-image':
        (tmp_path / 'intel/ibt-1040-0041.sfi').unlink()
    if fault == 'corrupt-image':
        path = tmp_path / 'intel/ibt-1040-0041.sfi'
        with path.open('r+b') as image:
            image.write(b'bad!')
    env = dict(os.environ, BLUEHEAD_USB_FAULT='intel-' + fault)
    result = subprocess.run([str(intel_probe), str(tmp_path)], capture_output=True, text=True,
                            encoding='utf-8', env=env, timeout=14, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert 'error' not in report, report
    assert report['start_milliseconds'] < 100, report
    assert report['fake_bad_release'] == report['fake_pending'] == report['fake_bulk_violations'] == 0, report
    assert report['closed'], report
    if fault in ('cold', 'warm', 'fragmented', 'boot-metadata'):
        assert report['active'] and not report['startup']['error'], report
        assert report['reopened']['firmware_state'] == 'already_loaded', report
        assert report['fake_firmware_loads'] == (0 if fault == 'warm' else 1), report
        assert report['fake_upload_commands'] == 0 if fault == 'warm' else report['fake_upload_commands'] > 2800
    elif fault == 'cancel':
        assert report['startup']['starting'] and not report['active'], report
        assert report['seconds'] < 1, report
    else:
        assert not report['active'] and report['startup']['error'].startswith(
            'Controller identity:' if fault == 'ambiguous' else 'Intel firmware:'), report
        if fault in ('missing-image', 'corrupt-image', 'unsupported', 'ambiguous'):
            assert report['fake_upload_commands'] == 0, report


def test_distribution_uses_one_pinned_firmware_pair_with_explicit_upstream_aliases():
    assert len(verify()) == 2
    origin = json.loads((ROOT / 'vendor/intel-firmware/origin.json').read_text('utf-8'))
    header = (ROOT / 'native/transport/intel_firmware.hpp').read_text('utf-8')
    for name, file in origin['files'].items():
        assert file['sha256'] in header and str(file['bytes']) in header
    assert 'ibt-1040-0041' in origin['aliases'] and 'ibt-0040-0041' in origin['aliases']


@pytest.mark.parametrize('extra,reason', [(['--offline-test'], 'offline'), ([], 'helper parent')])
def test_driver_firmware_tool_rejects_unsafe_invocation_before_opening_usb(extra, reason):
    process = subprocess.run([str(ROOT / 'bluehead/assets/BlueHead.Runtime.exe'), '--prepare-controller', *extra],
                             capture_output=True, text=True, encoding='utf-8', timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode == 2 and reason in process.stderr
