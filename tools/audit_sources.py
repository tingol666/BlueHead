"""Check the GitHub/source-archive allowlist without publishing or deleting files."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import finalize_release as sources  # noqa: E402


def audit():
    public = {relative.as_posix(): path for path, relative in sources.public_sources()}
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    unwanted = sorted(name for name in tracked if name and name not in public)
    untracked = sorted(name for name in public if name not in set(tracked))
    return dict(passed=not unwanted and not untracked, public_files=len(public),
                public_bytes=sum(path.stat().st_size for path in public.values()),
                unnecessary_tracked=unwanted, public_not_tracked=untracked,
                retained_binary_dependencies=sorted(sources.SOURCE_BINARIES),
                scope='Current source tree/index; no Git history rewriting and no publishing')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    result = audit()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
