"""Optional upstream payloads stay in an ignored, explicitly provisioned cache."""
import hashlib
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='session')
def firmware_sample():
    catalog = json.loads((ROOT / 'bluehead/assets/firmware/catalog.json').read_text('utf-8'))
    directory = Path(os.environ.get('BLUEHEAD_TEST_FIRMWARE', ROOT / 'work/firmware-fixtures'))

    def read(name):
        artifact = catalog['artifacts'][name]
        path = directory / artifact['sha256']
        if not path.is_file():
            pytest.skip('Optional upstream firmware fixture absent; see docs/TESTING.md')
        raw = path.read_bytes()
        assert len(raw) == artifact['bytes'] and hashlib.sha256(raw).hexdigest() == artifact['sha256']
        return raw

    return read
