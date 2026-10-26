"""Read-only Windows package audit; parses PE/PYZ files without executing them.

Requires the locked development dependencies pefile and PyInstaller. Reports use
package-relative names only. This is static evidence, not a hardware/startup test.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tomllib

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = {"avcodec-63.dll", "avutil-61.dll", "swresample-7.dll", "bluehead_plc.dll"}
PRIVATE = {"data", "work", "local", ".venv", ".git", "archive", "pairing.json", "settings.json",
           "recovery.json", "desktop.json"}
REQUIRED_LICENSES = {
    "python/LICENSE.txt", "pyinstaller/COPYING.txt", "ffmpeg/COPYING.LGPLv2.1",
    "libwdi/COPYING-LGPL", "libusb/COPYING", "spandsp-plc/COPYING", "miniaudio/LICENSE",
    "speexdsp/COPYING", "liblc3/LICENSE", "pipewire-rate-control/LICENSE", "gcc-runtime/COPYING.RUNTIME",
    "winpthreads/COPYING", "dotnet/LICENSE.TXT", "wpf-ui/LICENSE.md", "wpf-ui-abstractions/LICENSE.md",
    "libldacdec-MIT.txt", "nlohmann-json/LICENSE.MIT",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def forbidden(name):
    name = name.casefold()
    return (name in {"av", "av.libs", "numpy", "numpy.libs", "sounddevice", "sounddevice_data", "_sounddevice",
                     "coreclr.dll", "hostfxr.dll", "hostpolicy.dll"} or
            name.startswith(("numpy.", "sounddevice.", "_portaudio", "portaudio", "libportaudio", "libopenblas",
                             "openblas", "microsoft.netcore.app")))


def audit(package):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    expected_version = tuple(int(part) for part in version.split(".")) + (0,)
    internal = package / "app/engine/_internal"
    files = sorted(path for path in package.rglob("*") if path.is_file())
    errors, checks, versions, native_hashes = [], {}, {}, {}

    def fail(check, component, reason):
        errors.append({"check": check, "component": component, "reason": reason})

    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(package):
            fail("safe_material", relative.as_posix(), "Linked or escaped package material")
        if any(part.casefold() in PRIVATE for part in relative.parts) or path.suffix.casefold() in {".log", ".dmp", ".key", ".pfx", ".pem"}:
            fail("safe_material", relative.as_posix(), "Private material")
        if any(forbidden(part) for part in relative.parts):
            fail("retired_dependencies", relative.as_posix(), "Retired DSP/runtime component")

    by_name = {}
    for path in files:
        by_name.setdefault(path.name.casefold(), []).append(path)
        name = path.name.casefold()
        if name.startswith(("avcodec-", "avutil-", "swresample-", "avformat-", "avfilter-", "avdevice-", "swscale-")) and name not in CANONICAL:
            fail("canonical_libraries", path.relative_to(package).as_posix(), "Unexpected FFmpeg component")
    for name in CANONICAL:
        matches = by_name.get(name, [])
        if matches != [internal / name]:
            fail("canonical_libraries", name, "Expected exactly one copy in app/engine/_internal")
    for asset in sorted((ROOT / "bluehead/assets").glob("*.dll")):
        matches = by_name.get(asset.name.casefold(), [])
        if asset.name == "libwdi.dll":
            expected = package / "app" / asset.name
        elif asset.name in CANONICAL | {"libwinpthread-1.dll"}:
            expected = internal / asset.name
        else:
            expected = internal / "bluehead/assets" / asset.name
        if expected not in matches:
            fail("native_asset_hashes", asset.name, "Required native asset missing at its runtime lookup location")
        for path in matches:
            matches_asset = digest(path) == digest(asset)
            native_hashes[path.relative_to(package).as_posix()] = matches_asset
            if not matches_asset:
                fail("native_asset_hashes", path.relative_to(package).as_posix(), "Differs from the source native asset")

    runtime = package / "app/engine/BlueHead.Runtime.exe"
    runtime_matches = by_name.get("bluehead.runtime.exe", [])
    if runtime_matches != [runtime]:
        fail("native_runtime", "BlueHead.Runtime.exe", "Expected one native owner at app/engine")
    elif digest(runtime) != digest(ROOT / "bluehead/assets/BlueHead.Runtime.exe"):
        fail("native_runtime", "BlueHead.Runtime.exe", "Native runtime differs from the source asset")

    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    system = (windows / "System32", windows)
    pe_count = 0
    import_count = {"package": 0, "windows_system": 0, "windows_api_set": 0}
    for path in files:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                continue
        relative = path.relative_to(package).as_posix()
        try:
            with pefile.PE(str(path), fast_load=False) as image:
                pe_count += 1
                if path.suffix.casefold() == ".exe":
                    values = getattr(image, "VS_FIXEDFILEINFO", [])
                    found = {(item.FileVersionMS >> 16, item.FileVersionMS & 65535,
                              item.FileVersionLS >> 16, item.FileVersionLS & 65535) for item in values}
                    versions[relative] = [list(value) for value in sorted(found)]
                    if found != {expected_version}:
                        fail("executable_versions", relative, "Missing or inconsistent project version")
                managed = bool(image.OPTIONAL_HEADER.DATA_DIRECTORY[14].VirtualAddress)
                if not managed and image.FILE_HEADER.Machine != 0x8664:
                    fail("native_x64", relative, "Native component is not AMD64")
                scope = [path.parent]
                if path.is_relative_to(internal):
                    # PyInstaller adds _internal as the process DLL directory.
                    # A nested DLL's own directory is considered for its explicit
                    # same-directory native dependencies, never an arbitrary PATH.
                    scope.append(internal)
                elif path == package / "app/engine/BlueHead.Engine.exe":
                    scope.append(internal)
                imports = list(getattr(image, "DIRECTORY_ENTRY_IMPORT", []))
                imports += list(getattr(image, "DIRECTORY_ENTRY_DELAY_IMPORT", []))
                for entry in imports:
                    name = entry.dll.decode("ascii", errors="strict")
                    if any((directory / name).is_file() for directory in scope):
                        import_count["package"] += 1
                    elif name.casefold().startswith(("api-ms-win-", "ext-ms-win-")):
                        import_count["windows_api_set"] += 1
                    elif any((directory / name).is_file() for directory in system):
                        import_count["windows_system"] += 1
                    else:
                        fail("pe_import_closure", relative, "Unresolved import: " + name)
        except (pefile.PEFormatError, UnicodeError) as error:
            fail("valid_pe", relative, type(error).__name__)

    required_exes = {"BlueHead.exe", "app/BlueHead.exe", "app/BlueHead.DriverHelper.exe",
                     "app/engine/BlueHead.Engine.exe"}
    for name in required_exes - versions.keys():
        fail("executable_versions", name, "Required executable missing")
    try:
        frozen = CArchiveReader(str(package / "app/engine/BlueHead.Engine.exe"))
        modules = frozen.open_embedded_archive("PYZ.pyz").toc
        checks["frozen_module_count"] = len(modules)
        for name in modules:
            if any(forbidden(part) for part in name.split(".")):
                fail("retired_dependencies", name, "Retired module embedded in PYZ")
    except (OSError, KeyError, ValueError) as error:
        fail("frozen_module_inventory", "BlueHead.Engine.exe", type(error).__name__)

    licenses = package / "licenses"
    for name in REQUIRED_LICENSES:
        path = licenses / name
        if not path.is_file() or not path.stat().st_size:
            fail("licenses", "licenses/" + name, "Required license missing or empty")
    records = json.loads((ROOT / "dependencies.json").read_text("utf-8"))
    license_dirs = {path.name.casefold().replace("_", "-") for path in licenses.iterdir() if path.is_dir()}
    for item in records:
        name = item["name"].casefold().replace("_", "-")
        if item.get("scope") == "runtime" and name not in license_dirs:
            fail("licenses", "licenses/" + name, "Runtime dependency license directory missing")

    checks.update({"pe_files": pe_count, "pe_imports": import_count, "executable_versions": versions,
                   "native_asset_hashes": native_hashes, "required_license_files": len(REQUIRED_LICENSES)})
    return {"version": version, "passed": not errors, "scope": "Static package PE/PYZ, asset, version and license audit; no binaries loaded or executed",
            "limitations": "PE imports and known dynamic assets only; managed references/COM, API-set availability on older Windows and hardware need separate acceptance",
            "files": len(files), "bytes": sum(path.stat().st_size for path in files), "checks": checks, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Complete BlueHead assembled root")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    report = args.report.resolve()
    if report.is_relative_to(package):
        raise SystemExit("Write the audit report outside the read-only audited package")
    result = audit(package)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({"version": result["version"], "passed": result["passed"], "files": result["files"],
                      "pe_files": result["checks"]["pe_files"], "errors": result["errors"]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
