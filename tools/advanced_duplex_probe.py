"""External quiet tone + actual local duplex; preserve only numeric results."""
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.loopback_probe import tone  # noqa: E402
from bluehead.audio_runtime import on_audio_thread  # noqa: E402

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    stream = tone(941)
    try:
        result = subprocess.run([sys._base_executable, str(root / "tools/duplex_thread_probe.py"),
                                 str(root / "work/advanced-duplex-beta4.json"), "--system-mix"], cwd=root,
                                env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    finally:
        on_audio_thread(stream.close)()
    raise SystemExit(result.returncode)
