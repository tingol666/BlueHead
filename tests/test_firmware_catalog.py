"""Production CNG verifier, content store and acquisition isolation; no hardware."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from tools.native_build import compiler, includes

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'bluehead/assets'
RUNTIME = ASSETS / 'BlueHead.Runtime.exe'


def run(*arguments):
    return subprocess.run([str(RUNTIME), *map(str, arguments)], capture_output=True, text=True, encoding='utf-8',
                          timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.fixture(scope='module')
def selector(tmp_path_factory):
    output = tmp_path_factory.mktemp('catalog-selector') / 'selector.exe'
    subprocess.run([compiler(cxx=True), '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-municode', '-static',
                    *includes(ROOT / 'work/runtime-build', ROOT / 'vendor/nlohmann-json'),
                    str(ROOT / 'tests/native_firmware_catalog.cpp'), '-o', str(output), '-lbcrypt', '-lavrt'], check=True)
    return output


@pytest.mark.parametrize('backend,facts,expected', [
    ('intel-tlv', {'variant': 23, 'cnvi': 64, 'cnvr': 65}, 'intel-tlv-0041'),
    ('intel-tlv', {'variant': 25, 'cnvi': 4160, 'cnvr': 16720}, 'intel-tlv-0041'),
    ('intel-tlv', {'variant': 24, 'cnvi': 4160, 'cnvr': 4176}, 'intel-tlv-0041'),
    ('intel-tlv', {'variant': 1, 'cnvi': 64, 'cnvr': 65}, None),
    ('intel-tlv', {'variant': 23, 'cnvi': 1, 'cnvr': 65}, None),
    ('intel-tlv', {'variant': 23, 'cnvi': 64}, None),
    ('realtek', {'usb': [3034, 34673], 'rom': 34657, 'revision': 11, 'hci': 10, 'manufacturer': 93}, 'realtek-8761bu'),
    ('realtek', {'usb': [3034, 34673], 'rom': 34657, 'revision': 11, 'hci': 10, 'manufacturer': 2}, None),
    ('realtek', {'usb': [3034, 1], 'rom': 34657, 'revision': 11, 'hci': 10, 'manufacturer': 93}, None),
    ('generic', {'usb': [3034, 34673]}, None),
])
def test_chip_and_board_constraints_are_mandatory(selector, backend, facts, expected):
    result = subprocess.run([str(selector), str(ASSETS), backend, json.dumps(facts)], capture_output=True, text=True,
                            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    if expected:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['id'] == expected
    else:
        assert result.returncode == 2 and 'no validated firmware' in result.stderr


@pytest.mark.parametrize('fault', ['catalog', 'signature', 'missing-signature', 'signature-size', 'oversize-catalog'])
def test_tampered_catalog_never_resolves_or_downloads(tmp_path, fault):
    shutil.copytree(ASSETS / 'firmware', tmp_path / 'firmware')
    if fault == 'catalog':
        with (tmp_path / 'firmware/catalog.json').open('ab') as file:
            file.write(b' ')
    elif fault == 'signature':
        signature = tmp_path / 'firmware/catalog.sig'
        raw = bytearray(signature.read_bytes())
        raw[0] ^= 1
        signature.write_bytes(raw)
    elif fault == 'missing-signature':
        (tmp_path / 'firmware/catalog.sig').unlink()
    elif fault == 'signature-size':
        (tmp_path / 'firmware/catalog.sig').write_bytes(bytes(63))
    else:
        (tmp_path / 'firmware/catalog.json').write_bytes(bytes(1024 * 1024 + 1))
    result = run('--assets', tmp_path, '--firmware', 'ensure', '--firmware-profile', 'intel-tlv-0041', '--firmware-cache', tmp_path / 'cache')
    assert result.returncode == 3
    assert not (tmp_path / 'cache').exists()


def test_acquisition_has_no_audio_usb_libraries_and_repairs_corrupt_cache(tmp_path, firmware_sample):
    # Assets contain metadata/licenses only; payloads arrive through exact import.
    shutil.copytree(ASSETS / 'firmware', tmp_path / 'firmware')
    imported = tmp_path / 'import'
    imported.mkdir()
    cache = tmp_path / 'cache'
    cache.mkdir()
    catalog = json.loads((tmp_path / 'firmware/catalog.json').read_text('utf-8'))
    for name, artifact in catalog['artifacts'].items():
        if 'bundled' in artifact:
            (cache / artifact['sha256']).write_bytes(b'corrupt')
        if name in ('intel-sfi', 'intel-ddc'):
            (imported / Path(artifact['path']).name).write_bytes(firmware_sample(name))
            (cache / artifact['sha256']).write_bytes(b'corrupt')
    result = run('--assets', tmp_path, '--firmware', 'ensure', '--firmware-profile', 'intel-tlv-0041', '--firmware-cache', cache,
                 '--firmware-import', imported)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready']


def test_offline_migration_survives_removal_of_old_application(tmp_path, firmware_sample):
    assets, imported, cache = tmp_path / 'assets', tmp_path / 'old-app', tmp_path / 'cache'
    shutil.copytree(ASSETS / 'firmware', assets / 'firmware')
    (imported / 'intel').mkdir(parents=True)
    catalog = json.loads((assets / 'firmware/catalog.json').read_text('utf-8'))
    for name in ('intel-sfi', 'intel-ddc'):
        artifact = catalog['artifacts'][name]
        (imported / artifact['path']).write_bytes(firmware_sample(name))
    arguments = ('--assets', assets, '--firmware-cache', cache)
    result = run(*arguments, '--firmware', 'migrate', '--firmware-import', imported)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready_profiles'] == ['intel-tlv-0041']
    shutil.rmtree(imported)
    result = run(*arguments, '--firmware', 'ensure', '--firmware-profile', 'intel-tlv-0041')
    assert result.returncode == 0 and json.loads(result.stdout)['ready'], result.stderr
    for name in ('intel-sfi', 'intel-ddc', 'intel-license'):
        artifact = catalog['artifacts'][name]
        assert hashlib.sha256((cache / artifact['sha256']).read_bytes()).hexdigest() == artifact['sha256']


def test_migration_reports_missing_payload_without_claiming_profile_ready(tmp_path):
    result = run('--firmware', 'migrate', '--firmware-cache', tmp_path / 'cache', '--firmware-import', tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready_profiles'] == []


def test_migration_rejects_modified_present_payload(tmp_path):
    (tmp_path / 'ibt-1040-0041.sfi').write_bytes(b'bad')
    result = run('--firmware', 'migrate', '--firmware-cache', tmp_path / 'cache', '--firmware-import', tmp_path)
    assert result.returncode == 3 and 'digest or length' in result.stderr
    catalog = json.loads((ASSETS / 'firmware/catalog.json').read_text('utf-8'))
    assert not (tmp_path / 'cache' / catalog['artifacts']['intel-sfi']['sha256']).exists()


def test_migration_never_enables_network_even_when_requested(tmp_path):
    result = run('--firmware', 'migrate', '--firmware-import', tmp_path, '--allow-network')
    assert result.returncode == 2 and 'offline only' in result.stderr


def test_wrong_import_digest_never_commits(tmp_path):
    folder = tmp_path / 'import'
    folder.mkdir()
    (folder / 'rtl8761bu_fw.bin').write_bytes(b'wrong')
    result = run('--firmware', 'ensure', '--firmware-profile', 'realtek-8761bu', '--firmware-cache', tmp_path / 'cache', '--firmware-import', folder)
    assert result.returncode == 3 and 'digest or length' in result.stderr
    artifact = json.loads((ASSETS / 'firmware/catalog.json').read_text('utf-8'))['artifacts']['rtl8761bu-image']
    assert not (tmp_path / 'cache' / artifact['sha256']).exists()


@pytest.mark.parametrize('mode', ['--engine', '--offline-test', '--prepare-controller'])
def test_firmware_worker_cannot_take_hci_or_audio_ownership(mode):
    result = run('--firmware', 'inspect', mode)
    assert result.returncode == 2 and 'audio/HCI host' in result.stderr


def test_catalog_sources_are_pinned_and_small_bundle_has_no_private_key():
    catalog = json.loads((ASSETS / 'firmware/catalog.json').read_text('utf-8'))
    assert len(catalog['profiles']) == 5
    assert not list((ASSETS / 'firmware').glob('*.pem'))
    for artifact in catalog['artifacts'].values():
        assert all(catalog['source_revision'] in source['url'] for source in artifact['sources'])
        if 'bundled' in artifact:
            raw = (ASSETS / artifact['bundled']).read_bytes()
            assert len(raw) == artifact['bytes'] and hashlib.sha256(raw).hexdigest() == artifact['sha256']
