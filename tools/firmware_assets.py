"""Validate the vendor-neutral base package: signed metadata and licenses only."""
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
    licenses = Path(licenses or ROOT / 'licenses')
    verify_catalog(assets / 'firmware')
    catalog = json.loads((assets / 'firmware/catalog.json').read_text('utf-8'))
    license_ids = {profile['license'] for profile in catalog['profiles']}
    payload_ids = {profile[role] for profile in catalog['profiles'] for role in ('image', 'config')}
    files = []
    for name, artifact in catalog['artifacts'].items():
        if name in payload_ids and ('bundled' in artifact or (assets / artifact['path']).exists()):
            raise ValueError('Firmware payload must not enter the base package: ' + name)
        if 'bundled' not in artifact:
            continue
        if name not in license_ids:
            raise ValueError('Only firmware licenses may be bundled: ' + name)
        path = assets / artifact['bundled']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('Missing or modified bundled firmware resource: ' + artifact['bundled'])
        if not any(hashlib.sha256(p.read_bytes()).hexdigest() == artifact['sha256']
                   for p in licenses.rglob(path.name) if p.is_file() and not p.is_symlink()):
            raise ValueError('Missing firmware redistribution terms: ' + path.name)
        files.append(path)
    for path in assets.rglob('*'):
        if path.suffix.casefold() in {'.sfi', '.ddc', '.fw', '.bin'}:
            raise ValueError('Unexpected firmware payload in base assets: ' + path.name)
    return [*files, assets / 'firmware/catalog.json', assets / 'firmware/catalog.sig']


if __name__ == '__main__':
    print('Verified signed firmware resources:', len(verify()))
