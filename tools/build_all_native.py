"""Build every native dependency from the clean checkout, without hardware I/O."""
import argparse
import hashlib
import json
import subprocess
import sys
from native_build import ROOT
from sync_version import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-ffmpeg', action='store_true', help='Reuse a verified local FFmpeg install')
    args = parser.parse_args()
    check()
    usb = ROOT / 'bluehead/assets/libusb-1.0.dll'
    origin = json.loads((ROOT / 'vendor/libusb/source.json').read_text('utf-8'))
    if hashlib.sha256(usb.read_bytes()).hexdigest() != origin['binary_sha256']:
        raise RuntimeError('Pinned libusb redistributable does not match its source record')
    steps = [] if args.skip_ffmpeg else [('build_ffmpeg', '--native-only')]
    steps += [(name,) for name in ('build_icon', 'build_native', 'build_audio', 'build_voice',
                                  'build_device', 'build_ldac_media', 'build_lhdc5', 'build_loopback',
                                  'build_trace', 'build_wdi', 'build_runtime')]
    for name, *arguments in steps:
        print('Building ' + name, flush=True)
        subprocess.run([sys.executable, str(ROOT / 'tools' / (name + '.py')), *arguments],
                       cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
