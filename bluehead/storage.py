"""Per-user state shared by the desktop and engine; portable mode is explicit."""
import os
from pathlib import Path


def data_directory():
    configured = os.environ.get("BLUEHEAD_DATA_DIR")
    if configured:
        return Path(configured).resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "BlueHead" / "data"
