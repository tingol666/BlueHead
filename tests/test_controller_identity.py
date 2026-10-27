"""Physical selection contract and production single-owner tests, without hardware."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from bluehead.controller import transport_target
from tools.native_build import ROOT, compiler, includes


@pytest.mark.parametrize('spec', ['usb-id:' + 'a' * 24, 'usb-id:' + 'a' * 24 + '+sco=0'])
def test_stable_selector_preserves_identity_and_ignores_voice_hint(spec):
    assert transport_target(spec) == {'adapter_id': 'a' * 24}


@pytest.mark.parametrize('spec', ['usb-id:../device', 'usb-id:' + 'A' * 24, 'usb-id:' + 'a' * 25,
                                'usb-id:' + 'a' * 24 + ';command', None, 'usb:1234:abcd#0\n'])
def test_untrusted_selector_rejected_before_native_owner(spec):
    with pytest.raises(ValueError):
        transport_target(spec)


@pytest.fixture(scope='module')
def controller_probe(tmp_path_factory):
    output = tmp_path_factory.mktemp('controller-identity')
    cc = str(Path(compiler(cxx=True)).with_name('gcc.exe'))
    header = ROOT / 'work/runtime-build'
    subprocess.run([cc, '-shared', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-Wl,--export-all-symbols', '-I', str(header), str(ROOT / 'tests/native_usb_fake.c'),
                    '-o', str(output / 'libusb-1.0.dll')], check=True)
    executable = output / 'controller-identity.exe'
    subprocess.run([compiler(cxx=True), '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-municode',
                    '-static', *includes(header, ROOT / 'vendor/nlohmann-json'),
                    str(ROOT / 'tests/native_controller_identity.cpp'), '-o', str(executable),
                    '-lbcrypt', '-lavrt'], check=True)
    return executable


@pytest.mark.parametrize('mode', ['normal', 'reorder', 'legacy'])
def test_identical_models_select_physical_instance_and_share_owner_lease(controller_probe, mode):
    result = subprocess.run([str(controller_probe), str(controller_probe.parent)], capture_output=True,
                            encoding='utf-8', timeout=8, creationflags=subprocess.CREATE_NO_WINDOW,
                            env=dict(os.environ, BLUEHEAD_USB_FAULT='identity-' + mode))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert 'error' not in report and report['invalid_releases'] == report['pending'] == 0, report
    assert report['canonical_case'] and report['distinct_instances'], report
    if mode == 'legacy':
        assert not report['active'] and 'ambiguous' in report['startup']['error'], report
    else:
        assert report['active'] and report['original_still_active'], report
        assert report['selected_device'] == 1, report
        assert not report['duplicate_active'] and 'HCI owner' in report['duplicate']['error'], report
        assert report['startup']['capabilities']['media_transport']
        assert not report['startup']['capabilities']['voice_verified']
