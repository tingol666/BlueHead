"""Create public archives, exact-file uninstaller and checksums from a clean build."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import subprocess
import zipfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
FILES = {".clang-format", ".gitattributes", ".gitignore", "README.md", "README.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt", "CHANGELOG.md",
         "CONTRIBUTING.md", "pyproject.toml", "requirements.lock.txt", "requirements-runtime.lock.txt", "dependencies.json",
         "global.json", "NuGet.Config", "launch.py", "engine_launch.py"}
FOLDERS = {".github", "bluehead", "desktop", "docs", "installer", "licenses", "native", "tests", "tools", "vendor"}
EXCLUDED = {"bin", "obj", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", "legacy",
            "data", "work", "local", ".venv", "archive", "build", "dist", "outputs"}
PRIVATE = {"pairing.json", "settings.json", "recovery.json", "desktop.json"}
PRIVATE_SUFFIXES = {".pyc", ".pdb", ".log", ".dmp", ".user", ".suo", ".bak", ".tmp",
                    ".key", ".pem", ".pfx", ".p12"}
GENERATED_SUFFIXES = {".dll", ".exe", ".a", ".o", ".obj", ".lib", ".sfi", ".ddc", ".fw", ".bin"}
SOURCE_BINARIES = {"bluehead/assets/libusb-1.0.dll"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_path(relative):
    """Windows names are case-insensitive, including in archives reviewed elsewhere."""
    parts = tuple(part.casefold() for part in relative.parts)
    return (any(part in EXCLUDED or part in PRIVATE for part in parts) or
            relative.suffix.casefold() in PRIVATE_SUFFIXES)


def generated_source(relative):
    name = relative.as_posix().casefold()
    if relative.name.casefold().startswith(("copying", "license", "notice")):
        return False
    return (name == "vendor/ffmpeg/build.json" or
            (relative.suffix.casefold() in GENERATED_SUFFIXES and name not in SOURCE_BINARIES))


def checked_location(path, boundary):
    # Never traverse a link/junction, even when its current target is internal.
    # This also avoids packaging a different target after an operator changes it.
    if path.is_symlink() or path.is_junction():
        raise RuntimeError("Linked material is not publishable: " + str(path))
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(boundary):
        raise RuntimeError("Publish material escapes its root: " + str(path))


def tree_files(directory, boundary, *, source):
    checked_location(directory, boundary)
    for path in sorted(directory.iterdir()):
        relative = path.relative_to(boundary)
        if private_path(relative):
            if source:
                continue
            raise RuntimeError("Private or build material in assembled release: " + str(relative))
        if source and generated_source(relative):
            continue
        checked_location(path, boundary)
        if path.is_dir():
            yield from tree_files(path, boundary, source=source)
        elif path.is_file():
            yield path
        else:
            raise RuntimeError("Unsupported publish material: " + str(path))


def public_sources():
    boundary = ROOT.resolve(strict=True)
    # Inspect only the allowed roots, and prune private/cache directories before
    # descending. No whole-checkout recursion or dependence on .gitignore.
    for name in sorted(FILES):
        file = ROOT / name
        if file.exists() or file.is_symlink():
            checked_location(file, boundary)
            if not file.is_file():
                raise RuntimeError("Expected a source file: " + str(file))
            yield file, file.relative_to(boundary)
    for name in sorted(FOLDERS):
        folder = ROOT / name
        if folder.exists() or folder.is_symlink():
            if not folder.is_dir():
                raise RuntimeError("Expected a source directory: " + str(folder))
            for file in tree_files(folder, boundary, source=True):
                yield file, file.relative_to(boundary)


def preflight(release):
    """Validate all inputs before writing manifests, invoking NSIS or creating ZIPs."""
    portable = release / "BlueHead"
    checked_location(portable, release)
    if not portable.is_dir():
        raise RuntimeError("Missing assembled release directory")
    if not (portable / "app/engine/BlueHead.Engine.exe").is_file():
        raise RuntimeError("Missing assembled engine executable")
    report = release / "VALIDATION.md"
    if not report.is_file():
        raise RuntimeError("Write actual VALIDATION.md status before releasing")
    checked_location(report, release)
    public = list(tree_files(portable, portable.resolve(strict=True), source=False))
    if any(file.relative_to(portable).as_posix().casefold() == "portable.flag" for file in public):
        raise RuntimeError("Assembled installer tree must not contain a portable marker")
    sources = list(public_sources())
    return portable, public, sources


def validate_archive(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Archive CRC validation failed: " + str(path))
        for name in archive.namelist():
            relative = PurePosixPath(name.replace("\\", "/"))
            if (relative.is_absolute() or not relative.parts or relative.parts[0] != "BlueHead" or
                    any(part in {".", ".."} or ":" in part for part in relative.parts) or private_path(relative)):
                raise RuntimeError("Unsafe member in archive: " + str(path) + ": " + name)


def nsis_literal(text):
    return text.replace("$", "$$").replace('"', '$\\"')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--nsis", type=Path, required=True)
    args = parser.parse_args()
    release = args.release.resolve()
    portable, public, sources = preflight(release)
    (portable / "VALIDATION.md").write_bytes((release / "VALIDATION.md").read_bytes())
    public = sorted(file for file in tree_files(portable, portable.resolve(strict=True), source=False)
                    if file.name != "manifest.sha256")
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
        for file, relative in sources:
            archive.write(file, "BlueHead/" + relative.as_posix())
    artifacts = [installer, portable_zip, source_zip, release / "VALIDATION.md"]
    # Evidence is a separate download, never embedded in the application.
    evidence = release / f"BlueHead-{VERSION}-Checks.zip"
    if evidence.is_file():
        checked_location(evidence, release)
        with zipfile.ZipFile(evidence) as archive:
            if archive.testzip():
                raise RuntimeError("Evidence archive CRC validation failed: " + evidence.name)
        artifacts.append(evidence)
    # Read every compressed member to verify CRC, then enforce public boundaries.
    for path in (portable_zip, source_zip):
        validate_archive(path)
    (release / "SHA256SUMS.txt").write_text("".join(digest(file) + "  " + file.name + "\n" for file in artifacts), "utf-8")
    print("\n".join(str(file) for file in artifacts))


if __name__ == "__main__":
    main()
