"""Offline verification and explicit maintainer signing; never used in audio callbacks."""
import argparse
import re
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.exceptions import InvalidSignature

ROOT = Path(__file__).resolve().parents[1]


def verify(directory=None):
    directory = Path(directory or ROOT / 'bluehead/assets/firmware')
    header = (ROOT / 'native/firmware/catalog_trust.hpp').read_text('utf-8')
    blob = bytes(int(value, 16) for value in re.findall(r'0x([0-9a-f]{2})', header))
    if len(blob) != 72 or blob[:8] != bytes.fromhex('4543533120000000'):
        raise ValueError('Invalid catalog trust key')
    public = ec.EllipticCurvePublicNumbers(int.from_bytes(blob[8:40], 'big'), int.from_bytes(blob[40:], 'big'), ec.SECP256R1()).public_key()
    signature = (directory / 'catalog.sig').read_bytes()
    if len(signature) != 64:
        raise ValueError('Invalid catalog signature')
    try:
        public.verify(utils.encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big')),
                      (directory / 'catalog.json').read_bytes(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as error:
        raise ValueError('Firmware catalog signature verification failed') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'bluehead/assets/firmware')
    parser.add_argument('--sign-key', type=Path, help='Explicit external P-256 key; keep outside published sources')
    args = parser.parse_args()
    if args.sign_key:
        # Byte-stable across Windows/Linux checkouts; catalog assets are -text.
        catalog_file = args.directory / 'catalog.json'
        catalog_file.write_bytes(catalog_file.read_bytes().replace(b'\r\n', b'\n'))
        key = serialization.load_pem_private_key(args.sign_key.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError('P-256 private key required')
        r, s = utils.decode_dss_signature(key.sign((args.directory / 'catalog.json').read_bytes(), ec.ECDSA(hashes.SHA256())))
        (args.directory / 'catalog.sig').write_bytes(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))
    verify(args.directory)
    print('Firmware catalog signature verified')


if __name__ == '__main__':
    main()
