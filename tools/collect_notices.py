"""Collect actual dependency versions and their installed license texts."""
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import ctypes
import os
import sys
import hashlib

root = Path(__file__).resolve().parents[1]
licenses = root / "licenses"
licenses.mkdir(exist_ok=True)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise FileNotFoundError("The bundled Python runtime's LICENSE.txt is required")
(licenses / "python").mkdir(exist_ok=True)
shutil.copy2(python_license, licenses / "python/LICENSE.txt")
# Native sources and their licenses are already vendored. Do not depend on a
# previous chat's temporary clones when refreshing a standalone checkout.
for required in (root / "LICENSE", licenses / "libldacdec-MIT.txt", root / "vendor/libldac_encoder/NOTICE", licenses / "pyserial/LICENSE.txt"):
    if not required.exists():
        raise FileNotFoundError(required)
serial_origin = json.loads((licenses / "pyserial/origin.json").read_text("utf-8"))
if hashlib.sha256((licenses / "pyserial/LICENSE.txt").read_bytes()).hexdigest() != serial_origin["sha256"]:
    raise RuntimeError("The pinned pySerial license was modified")
lines, records = [], []
development_only = {"altgraph", "av", "numpy", "sounddevice", "iniconfig", "packaging", "pefile", "pluggy",
                    "pygments", "pyinstaller", "pyinstaller-hooks-contrib", "pytest", "pytest-asyncio", "ruff", "setuptools",
                    "pywin32-ctypes"}
locked = {line.split("==")[0].split(" @ ")[0].lower().replace("_", "-")
          for line in (root / "requirements.lock.txt").read_text("utf-8").splitlines() if line.strip()}
for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    name, version = distribution.metadata["Name"], distribution.version
    if name.lower().replace("_", "-") not in locked:
        continue
    if name.lower() == "bumble":
        line = "bumble @ git+https://github.com/google/bumble.git@e1fe43c9b44be0d974a6fd533ac6d494fc50510a"
    else:
        line = f"{name}=={version}"
    lines.append(line)
    license_value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "See bundled license text"
    record = {"name": name, "version": version, "license": license_value, "project_urls": distribution.metadata.get_all("Project-URL", [])}
    record["scope"] = "development" if name.lower().replace("_", "-") in development_only else "runtime"
    records.append(record)
    for file in distribution.files or []:
        low = str(file).lower()
        if file.name.lower().startswith(("license", "copying", "notice")) and not low.endswith((".pyc", ".py")):
            source = distribution.locate_file(file)
            if source.is_file():
                destination = licenses / name / file.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
(root / "requirements.lock.txt").write_text("\n".join(lines) + "\n", "utf-8")
(root / "requirements-runtime.lock.txt").write_text("\n".join(
    line for line in lines if line.split("==")[0].split(" @ ")[0].lower().replace("_", "-") not in development_only
) + "\n", "utf-8")
(root / "dependencies.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
assets = root / "bluehead/assets"
library_records = {}
with os.add_dll_directory(str(assets)):
    for name, prefix in (("avcodec-63.dll", "avcodec"), ("avutil-61.dll", "avutil"), ("swresample-7.dll", "swresample")):
        api = ctypes.CDLL(str(assets / name))
        license_function = getattr(api, prefix + "_license")
        configuration_function = getattr(api, prefix + "_configuration")
        version_function = getattr(api, prefix + "_version")
        license_function.restype = configuration_function.restype = ctypes.c_char_p
        version_function.restype = ctypes.c_uint
        version = version_function()
        library_records[prefix] = {"version": [version >> 16, (version >> 8) & 255, version & 255],
                                   "license": license_function().decode("utf-8"),
                                   "configuration": configuration_function().decode("utf-8")}
        if not library_records[prefix]["license"].startswith("LGPL"):
            raise RuntimeError("Unexpected runtime FFmpeg license")
(root / "ffmpeg-build.json").write_text(json.dumps(library_records, indent=2), "utf-8")
for folder, names in {"miniaudio": ["LICENSE"], "speexdsp": ["COPYING"], "liblc3": ["LICENSE"], "pipewire-rate-control": ["LICENSE"],
                      "nlohmann-json": ["LICENSE.MIT"], "spandsp-plc": ["COPYING", "AUTHORS"]}.items():
    destination = licenses / folder
    destination.mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(root / "vendor" / folder / name, destination / name)
# THIRD_PARTY_NOTICES.txt is maintained with the corresponding native sources.
if not (root / "THIRD_PARTY_NOTICES.txt").exists():
    raise FileNotFoundError("THIRD_PARTY_NOTICES.txt")
print(f"Recorded {len(records)} dependency distributions and their license files")
