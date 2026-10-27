"""Bootloader, warm restart, corrupt assets, packet fragmentation and cancellation.

These run the actual C++ USB owner and signed image plan with an offline USB
model. No physical adapter, original-driver backup or system state is touched.
"""
import json
import base64
import gzip
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
    shutil.copytree(ROOT / 'bluehead/assets/firmware', tmp_path / 'firmware')
    if fault == 'missing-image':
        (tmp_path / 'intel/ibt-1040-0041.sfi').unlink()
    if fault == 'corrupt-image':
        path = tmp_path / 'intel/ibt-1040-0041.sfi'
        with path.open('r+b') as image:
            image.write(b'bad!')
    env = dict(os.environ, BLUEHEAD_USB_FAULT='intel-' + fault, LOCALAPPDATA=str(tmp_path / 'local'))
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
        assert not report['active'], report
        if fault == 'ambiguous':
            assert report['startup']['error'].startswith('Controller identity:'), report
        else:
            assert report['startup']['error'].startswith(('Intel firmware:', 'Firmware:')), report
        if fault in ('missing-image', 'corrupt-image', 'unsupported', 'ambiguous'):
            assert report['fake_upload_commands'] == 0, report


def test_distribution_uses_one_pinned_firmware_pair_with_explicit_upstream_aliases():
    assert len(verify()) == 6
    origin = json.loads((ROOT / 'vendor/intel-firmware/origin.json').read_text('utf-8'))
    header = (ROOT / 'bluehead/assets/firmware/catalog.json').read_text('utf-8')
    for name, file in origin['files'].items():
        assert file['sha256'] in header and str(file['bytes']) in header
    assert 'ibt-1040-0041' in origin['aliases'] and 'ibt-0040-0041' in origin['aliases']


@pytest.mark.parametrize('fault', ['cold', 'unknown-board', 'bad-ack', 'download-timeout'])
def test_realtek_download_is_board_matched_and_releases_one_owner(intel_probe, tmp_path, fault):
    shutil.copy2(intel_probe.parent / 'libusb-1.0.dll', tmp_path)
    shutil.copytree(ROOT / 'bluehead/assets/firmware', tmp_path / 'firmware')
    fixture = json.loads((ROOT / 'tests/fixtures/realtek-parser-vectors.json').read_text('utf-8'))['vectors'][0]
    catalog = json.loads((tmp_path / 'firmware/catalog.json').read_text('utf-8'))
    profile = next(row for row in catalog['profiles'] if row['id'] == 'realtek-8761bu')
    cache = tmp_path / 'local/BlueHead/Firmware'
    cache.mkdir(parents=True)
    for role in ('image', 'config', 'license'):
        artifact = catalog['artifacts'][profile[role]]
        raw = ((ROOT / 'licenses/realtek-bluetooth-firmware/LICENCE.rtlwifi_firmware.txt').read_bytes()
               if role == 'license' else gzip.decompress(base64.b64decode(fixture[role])))
        (cache / artifact['sha256']).write_bytes(raw)
    env = dict(os.environ, BLUEHEAD_USB_FAULT='realtek-' + fault, LOCALAPPDATA=str(tmp_path / 'local'))
    process = subprocess.run([str(intel_probe), str(tmp_path)], capture_output=True, text=True, encoding='utf-8', env=env,
                             timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
    report = json.loads(process.stdout)
    assert report['fake_bad_release'] == report['fake_pending'] == report['fake_bulk_violations'] == 0, report
    assert 'error' not in report and report['closed'], report
    if fault == 'cold':
        assert report['active'] and report['startup']['capabilities']['hci']['ready'], report
        assert report['fake_firmware_loads'] == 1 and report['fake_upload_commands'] > 50, report
    else:
        assert not report['active'] and report['startup']['firmware_error'], report
        if fault == 'unknown-board':
            assert report['fake_upload_commands'] == 0, report


@pytest.mark.parametrize('fault', ['ready', 'timeout', 'le-only', 'malformed', 'no-buffer', 'no-address'])
def test_standard_hci_must_be_operational_before_receiver_is_ready(intel_probe, tmp_path, fault):
    shutil.copy2(intel_probe.parent / 'libusb-1.0.dll', tmp_path)
    process = subprocess.run([str(intel_probe), str(tmp_path)], capture_output=True, text=True, encoding='utf-8',
                             env=dict(os.environ, BLUEHEAD_USB_FAULT='standard-' + fault), timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
    report = json.loads(process.stdout)
    assert report['fake_pending'] == report['fake_bad_release'] == 0 and report['closed'], report
    assert report['active'] == (fault == 'ready'), report
    if fault == 'ready':
        assert report['startup']['capabilities']['hci']['ready'] and report['startup']['firmware_state'] == 'ready'
        assert report['fake_upload_commands'] == 0
    else:
        assert report['startup']['firmware_error'], report


@pytest.mark.parametrize('fixture', json.loads((ROOT / 'tests/fixtures/realtek-parser-vectors.json').read_text('utf-8'))['vectors'], ids=lambda item: item['model'])
def test_realtek_parser_validates_all_ranges_before_any_usb_write(tmp_path, fixture):
    for name in ('image', 'config'):
        (tmp_path / name).write_bytes(gzip.decompress(base64.b64decode(fixture[name])))
    output = tmp_path / 'parser.exe'
    subprocess.run([compiler(cxx=True), '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-municode', '-static',
                    *includes(ROOT / 'work/runtime-build', ROOT / 'vendor/nlohmann-json'),
                    str(ROOT / 'tests/native_realtek_parser.cpp'), '-o', str(output), '-lbcrypt', '-lavrt'], check=True)
    process = subprocess.run([str(output), str(tmp_path / 'image'), str(tmp_path / 'config'), str(fixture['rom']), str(fixture['project'])], capture_output=True, text=True,
                             encoding='utf-8', timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    report = json.loads(process.stdout)
    assert process.returncode == 0 and report['passed'] and len(report['checks']) >= 30, report
    assert report['payload_sha256'] == fixture['expected_payload_sha256']


@pytest.mark.parametrize('extra,reason', [(['--offline-test'], 'offline'), ([], 'helper parent')])
def test_driver_firmware_tool_rejects_unsafe_invocation_before_opening_usb(extra, reason):
    process = subprocess.run([str(ROOT / 'bluehead/assets/BlueHead.Runtime.exe'), '--prepare-controller', *extra],
                             capture_output=True, text=True, encoding='utf-8', timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode == 2 and reason in process.stderr
