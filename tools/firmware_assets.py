"""Validate signed firmware metadata and the minimal bundled resource set."""
import hashlib
import json
from pathlib import Path
try:
    from firmware_catalog import verify as verify_catalog
except ModuleNotFoundError:
    from tools.firmware_catalog import verify as verify_catalog

ROOT = Path(__file__).resolve().parents[1]


def verify(assets=None, licenses=None):
    assets = Path(assets or ROOT / 'bluehead/assets')
    licenses = Path(licenses or ROOT / 'licenses') / 'intel-bluetooth-firmware'
    origin = json.loads((ROOT / 'vendor/intel-firmware/origin.json').read_text('utf-8'))
    files = []
    for name, expected in origin['files'].items():
        if Path(name).name != name:
            raise ValueError('Invalid fixed firmware asset name')
        path = assets / 'intel' / name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != expected['bytes'] or
                hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']):
            raise ValueError('Missing or modified Intel firmware: ' + name)
        files.append(path)
    license_file = licenses / 'LICENCE.ibt_firmware'
    if hashlib.sha256(license_file.read_bytes()).hexdigest() != origin['license_sha256']:
        raise ValueError('Modified Intel firmware redistribution terms')
    verify_catalog(assets / 'firmware')
    catalog = json.loads((assets / 'firmware/catalog.json').read_text('utf-8'))
    for artifact in catalog['artifacts'].values():
        if 'bundled' not in artifact:
            continue
        path = assets / artifact['bundled']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('Missing or modified bundled firmware resource: ' + artifact['bundled'])
        if path not in files:
            files.append(path)
    return [*files, assets / 'firmware/catalog.json', assets / 'firmware/catalog.sig']


if __name__ == '__main__':
    print('Verified signed firmware resources:', len(verify()))
