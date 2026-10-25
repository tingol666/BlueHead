"""Create public archives, exact-file uninstaller and checksums from a clean build."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import zipfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
FILES = {".gitignore", "README.md", "README.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt", "CHANGELOG.md",
         "CONTRIBUTING.md", "pyproject.toml", "requirements.lock.txt", "dependencies.json", "ffmpeg-build.json",
         "global.json", "NuGet.Config", "launch.py", "engine_launch.py"}
FOLDERS = {".github", "bluehead", "desktop", "docs", "installer", "licenses", "native", "tests", "tools", "vendor"}
EXCLUDED = {"bin", "obj", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", "legacy"}
PRIVATE = {"pairing.json", "settings.json", "recovery.json", "desktop.json"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def public_sources():
    candidates = [ROOT / name for name in FILES if (ROOT / name).is_file()]
    for folder in sorted(FOLDERS):
        candidates.extend((ROOT / folder).rglob("*"))
    for file in sorted(candidates):
        relative = file.relative_to(ROOT)
        # Traverse only allowed trees. Never package a whole working directory.
        if relative.parts[0] not in FOLDERS and relative.as_posix() not in FILES:
            continue
        if not file.is_file() or any(part in EXCLUDED for part in relative.parts):
            continue
        if file.name in PRIVATE or file.suffix.lower() in {".pyc", ".pdb", ".log", ".dmp", ".user", ".suo"}:
            continue
        yield file, relative


def nsis_literal(text):
    return text.replace("$", "$$").replace('"', '$\\"')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--nsis", type=Path, required=True)
    args = parser.parse_args()
    release = args.release.resolve()
    portable = release / "BlueHead"
    assert (portable / "app/engine/BlueHead.Engine.exe").is_file()
    assert (release / "VALIDATION.md").is_file(), "Write actual validation status before releasing"
    if (portable / "data").exists() or (portable / "work").exists() or (portable / "portable.flag").exists():
        raise SystemExit("Assembled installer tree must contain neither private state nor a portable marker")
    public = sorted(file for file in portable.rglob("*") if file.is_file())
    for file in public:
        if file.name in PRIVATE or file.suffix.lower() in {".log", ".dmp"}:
            raise RuntimeError("Private data in assembled release")
    (portable / "VALIDATION.md").write_bytes((release / "VALIDATION.md").read_bytes())
    public = sorted(file for file in portable.rglob("*") if file.is_file() and file.name != "manifest.sha256")
    (portable / "manifest.sha256").write_text("".join(digest(file) + "  " + file.relative_to(portable).as_posix() + "\n" for file in public), "utf-8")
    public.append(portable / "manifest.sha256")
    build = ROOT / "work/release-finalize"
    build.mkdir(parents=True, exist_ok=True)
    remove = build / "uninstall-files.nsh"
    lines = ['Delete "$INSTDIR\\' + nsis_literal(str(file.relative_to(portable))) + '"' for file in public]
    directories = sorted((p for p in portable.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
    lines += ['RMDir "$INSTDIR\\' + nsis_literal(str(path.relative_to(portable))) + '"' for path in directories]
    remove.write_text("\n".join(lines) + "\n", "utf-8-sig")
    installer = release / f"BlueHead-{VERSION}-Setup-x64.exe"
    subprocess.run([str(args.nsis.resolve()), "/V2", "/DAPP_VERSION=" + VERSION, "/DRELEASE_DIR=" + str(portable),
                    "/DOUTPUT_FILE=" + str(installer), "/DREMOVE_FILES=" + str(remove),
                    str(ROOT / "installer/BlueHead.nsi")], cwd=ROOT / "installer", check=True)
    portable_zip = release / f"BlueHead-{VERSION}-Portable-x64.zip"
    with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in public:
            archive.write(file, "BlueHead/" + file.relative_to(portable).as_posix())
        archive.writestr("BlueHead/portable.flag", "BlueHead portable mode; settings and pairing are stored in data/.\n")
    source_zip = release / f"BlueHead-{VERSION}-Source.zip"
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file, relative in public_sources():
            archive.write(file, "BlueHead/" + relative.as_posix())
    artifacts = [installer, portable_zip, source_zip, release / "VALIDATION.md"]
    (release / "SHA256SUMS.txt").write_text("".join(digest(file) + "  " + file.name + "\n" for file in artifacts), "utf-8")
    # Read every compressed member to verify CRC, then enforce public boundaries.
    for path in (portable_zip, source_zip):
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            for name in archive.namelist():
                parts = Path(name).parts
                assert not PRIVATE.intersection(parts)
                assert not {"local", "data", "work", ".venv", "archive", ".git"}.intersection(parts)
    print("\n".join(str(file) for file in artifacts))


if __name__ == "__main__":
    main()
