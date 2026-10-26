"""Public release input validation; never invokes NSIS or packages the checkout."""
from pathlib import Path
import sys
import zipfile

import pytest

from tools import finalize_release as release


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "README.md").write_text("Public project documentation", "utf-8")
    (root / "docs").mkdir()
    monkeypatch.setattr(release, "ROOT", root)
    return root


def assemble(tmp_path):
    directory = tmp_path / "candidate"
    engine = directory / "BlueHead/app/engine/BlueHead.Engine.exe"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"not an executable; input-validation fixture only")
    (directory / "VALIDATION.md").write_text("Pending real-device acceptance", "utf-8")
    return directory


def test_source_allowlist_prunes_private_cache_and_backup_material(source):
    for directory in release.EXCLUDED:
        hidden = source / "docs" / directory.upper()
        hidden.mkdir()
        (hidden / "innocent-name.txt").write_text("Never publish", "utf-8")
    for name in ("PAIRING.JSON", "Settings.Json", "Recovery.JSON", "DESKTOP.JSON",
                 "settings.tmp", "backup.BAK", "private.PEM", "dump.DMP", "trace.LOG"):
        (source / "docs" / name).write_text("Never publish", "utf-8")
    (source / "docs/guide.md").write_text("Public", "utf-8")
    (source / "unlisted.txt").write_text("Not allowlisted", "utf-8")
    selected = {relative.as_posix() for _, relative in release.public_sources()}
    assert selected == {"README.md", "docs/guide.md"}


def test_source_excludes_generated_binaries_but_keeps_pinned_external_runtime(source):
    assets = source / 'bluehead/assets'
    assets.mkdir(parents=True)
    for name in ('BlueHead.Runtime.exe', 'bluehead_audio.dll', 'libusb-1.0.dll'):
        (assets / name).write_bytes(b'Synthetic material, never executed')
    (source / 'docs/example.OBJ').write_bytes(b'Generated')
    (source / 'licenses/gcc').mkdir(parents=True)
    (source / 'licenses/gcc/COPYING.LIB').write_text('Required license text', 'utf-8')
    selected = {relative.as_posix() for _, relative in release.public_sources()}
    assert selected == {'README.md', 'bluehead/assets/libusb-1.0.dll', 'licenses/gcc/COPYING.LIB'}


@pytest.mark.parametrize("directory", ["Data", "WORK", "Local", ".VENV", "Archive", "BUILD", "Dist", "OUTPUTS"])
def test_installer_tree_rejects_private_directories_even_when_empty(source, tmp_path, directory):
    candidate = assemble(tmp_path)
    (candidate / "BlueHead/app" / directory).mkdir()
    with pytest.raises(RuntimeError, match="Private or build material"):
        release.preflight(candidate)


@pytest.mark.parametrize("name", ["Pairing.JSON", "SETTINGS.JSON", "Recovery.Json", "Desktop.JSON", "private.KEY"])
def test_installer_tree_rejects_private_filenames_case_insensitively(source, tmp_path, name):
    candidate = assemble(tmp_path)
    (candidate / "BlueHead/app" / name).write_text("Never publish", "utf-8")
    with pytest.raises(RuntimeError, match="Private or build material"):
        release.preflight(candidate)


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_source_links_and_junctions_are_rejected_without_traversal(source, monkeypatch, kind):
    linked = source / "docs/linked"
    linked.mkdir()
    original = getattr(Path, kind)
    monkeypatch.setattr(Path, kind, lambda path: path == linked or original(path))
    with pytest.raises(RuntimeError, match="Linked material"):
        list(release.public_sources())


def test_resolved_source_escape_is_rejected(source, tmp_path, monkeypatch):
    file = source / "docs/guide.md"
    file.write_text("Public", "utf-8")
    outside = tmp_path / "private-outside.md"
    outside.write_text("Never publish", "utf-8")
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: outside if path == file else original(path, **kwargs))
    with pytest.raises(RuntimeError, match="escapes its root"):
        list(release.public_sources())


def test_preflight_requires_report_and_rejects_installer_portable_marker(source, tmp_path):
    candidate = assemble(tmp_path)
    report = candidate / "VALIDATION.md"
    report.unlink()
    with pytest.raises(RuntimeError, match="actual VALIDATION.md"):
        release.preflight(candidate)
    report.write_text("Pending", "utf-8")
    (candidate / "BlueHead/Portable.Flag").write_text("Not an installer", "utf-8")
    with pytest.raises(RuntimeError, match="portable marker"):
        release.preflight(candidate)


def test_private_input_fails_before_any_public_artifact_or_nsis(source, tmp_path, monkeypatch):
    candidate = assemble(tmp_path)
    (candidate / "BlueHead/app/Settings.JSON").write_text("Never publish", "utf-8")
    invoked = []
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: invoked.append(args))
    monkeypatch.setattr(sys, "argv", ["finalize_release.py", str(candidate), "--nsis", str(tmp_path / "unused.exe")])
    with pytest.raises(RuntimeError, match="Private or build material"):
        release.main()
    assert invoked == []
    assert not (candidate / "BlueHead/VALIDATION.md").exists()
    assert not (candidate / "BlueHead/manifest.sha256").exists()
    assert {file.name for file in candidate.iterdir()} == {"BlueHead", "VALIDATION.md"}
    assert not (source / "work").exists()


def test_source_link_fails_before_manifest_or_nsis(source, tmp_path, monkeypatch):
    candidate = assemble(tmp_path)
    linked = source / "docs/linked.md"
    linked.write_text("Not publishable", "utf-8")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == linked or original(path))
    invoked = []
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: invoked.append(args))
    monkeypatch.setattr(sys, "argv", ["finalize_release.py", str(candidate), "--nsis", str(tmp_path / "unused.exe")])
    with pytest.raises(RuntimeError, match="Linked material"):
        release.main()
    assert invoked == []
    assert not (candidate / "BlueHead/VALIDATION.md").exists()
    assert not (candidate / "BlueHead/manifest.sha256").exists()
    assert {file.name for file in candidate.iterdir()} == {"BlueHead", "VALIDATION.md"}


@pytest.mark.parametrize("name", ["BlueHead/docs/WORK/private.txt", "BlueHead/SETTINGS.JSON",
                                  "BlueHead/app/private.PFX", "BlueHead/../outside.txt",
                                  "/BlueHead/README.md", "BlueHead/C:/private.txt",
                                  r"BlueHead\data\private.txt"])
def test_archive_postcheck_rejects_private_and_unsafe_names(tmp_path, name):
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(name, b"Fixture")
    with pytest.raises(RuntimeError, match="Unsafe member"):
        release.validate_archive(archive)


def test_archive_postcheck_allows_expected_portable_marker(tmp_path):
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("BlueHead/README.txt", b"Public")
        stream.writestr("BlueHead/portable.flag", b"Portable mode")
    release.validate_archive(archive)


def test_archive_crc_failure_is_an_explicit_exception(tmp_path, monkeypatch):
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("BlueHead/README.md", b"Public")
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "BlueHead/README.md")
    with pytest.raises(RuntimeError, match="CRC validation failed"):
        release.validate_archive(archive)
