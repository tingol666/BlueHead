"""Validate the fixed, unmodified Intel RAM-firmware distribution."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(assets=None, licenses=None):
    assets = Path(assets or ROOT / 'bluehead/assets') / 'intel'
    licenses = Path(licenses or ROOT / 'licenses') / 'intel-bluetooth-firmware'
    origin = json.loads((ROOT / 'vendor/intel-firmware/origin.json').read_text('utf-8'))
    files = []
    for name, expected in origin['files'].items():
        if Path(name).name != name:
            raise ValueError('Invalid fixed firmware asset name')
        path = assets / name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != expected['bytes'] or
                hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']):
            raise ValueError('Missing or modified Intel firmware: ' + name)
        files.append(path)
    license_file = licenses / 'LICENCE.ibt_firmware'
    if hashlib.sha256(license_file.read_bytes()).hexdigest() != origin['license_sha256']:
        raise ValueError('Modified Intel firmware redistribution terms')
    return files


if __name__ == '__main__':
    print('Verified Intel firmware assets:', len(verify()))
