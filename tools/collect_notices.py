"""Collect actual dependency versions and their installed license texts."""
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import av._core

root = Path(__file__).resolve().parents[1]
licenses = root / "licenses"
licenses.mkdir(exist_ok=True)
# Native sources and their licenses are already vendored. Do not depend on a
# previous chat's temporary clones when refreshing a standalone checkout.
for required in (root / "LICENSE", licenses / "libldacdec-MIT.txt", root / "vendor/libldac_encoder/NOTICE"):
    if not required.exists():
        raise FileNotFoundError(required)
lines, records = [], []
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
(root / "dependencies.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
(root / "ffmpeg-build.json").write_text(json.dumps(av._core.library_meta, indent=2), "utf-8")
# THIRD_PARTY_NOTICES.txt is maintained with the corresponding native sources.
if not (root / "THIRD_PARTY_NOTICES.txt").exists():
    raise FileNotFoundError("THIRD_PARTY_NOTICES.txt")
print(f"Recorded {len(records)} dependency distributions and their license files")
