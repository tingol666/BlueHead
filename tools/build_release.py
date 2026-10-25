"""Build/assemble a self-contained release without private state or driver changes."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.2.2"


def run(*args):
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def copy_tree(source, destination):
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.pdb", "*.log", "*.dmp", "__pycache__"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assemble-only", action="store_true")
    parser.add_argument("--desktop", type=Path, default=ROOT / "work/publish-desktop-final")
    parser.add_argument("--helper", type=Path, default=ROOT / "work/publish-helper-final")
    parser.add_argument("--engine", type=Path, default=ROOT / "work/engine-final-0.2/BlueHead.Engine")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "releases" / VERSION)
    args = parser.parse_args()
    release = args.output.resolve()
    portable = release / "BlueHead"
    if portable.exists():
        raise SystemExit("Output already exists. Choose a fresh release directory; never overwrite a running application.")
    if not args.assemble_only:
        for name in ("build_icon", "build_native", "build_audio", "build_loopback", "build_wdi", "build_ffmpeg", "collect_notices"):
            run(sys.executable, ROOT / "tools" / (name + ".py"))
        dotnet = shutil.which("dotnet") or str(ROOT / "work/toolchain/dotnet/dotnet.exe")
        for project, output in (("Desktop", args.desktop), ("DriverHelper", args.helper)):
            run(dotnet, "publish", ROOT / f"desktop/BlueHead.{project}/BlueHead.{project}.csproj", "-c", "Release",
                "-r", "win-x64", "--self-contained", "true", "-o", output, "-p:DebugType=None", "-p:DebugSymbols=false")
        run(sys.executable, ROOT / "tools/package_engine.py", args.engine.parent, ROOT / "work/build-engine-final-0.2")
    app = portable / "app"
    copy_tree(args.desktop, app)
    for source in args.helper.iterdir():
        if not source.is_file() or source.suffix == ".pdb":
            continue
        target = app / source.name
        if target.exists():
            # WPF publishes the implementation; the console helper only has
            # the small core-runtime forwarding facade. Keep WPF's assembly.
            if source.name == "WindowsBase.dll":
                continue
            if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                raise RuntimeError("Conflicting shared runtime: " + source.name)
        else:
            shutil.copy2(source, target)
    copy_tree(args.engine, app / "engine")
    for name in ("libwdi.dll", "bluehead.ico"):
        shutil.copy2(ROOT / "bluehead/assets" / name, app / name)
    run(sys.executable, ROOT / "tools/build_launcher.py", portable / "BlueHead.exe")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.txt", "README.txt"):
        shutil.copy2(ROOT / name, portable / name)
    copy_tree(ROOT / "licenses", portable / "licenses")
    # FFmpeg DLLs must be the explicitly rebuilt audio-only libraries.
    ffmpeg = app / "engine/_internal/av.libs"
    expected = {"avcodec-63.dll", "avdevice-63.dll", "avfilter-12.dll", "avformat-63.dll", "avutil-61.dll",
                "libwinpthread-1.dll", "swresample-7.dll", "swscale-10.dll"}
    if {p.name for p in ffmpeg.glob("*.dll")} != expected:
        raise RuntimeError("Unexpected FFmpeg libraries")
    for file in portable.rglob("*"):
        if file.name.lower() in {"pairing.json", "settings.json", "recovery.json"} or file.suffix in {".log", ".dmp"}:
            raise RuntimeError("Private data in release: " + str(file))
    # Marker is added only to the portable ZIP/deployment. Installer uses LocalAppData.
    manifest = "\n".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(portable).as_posix()
                         for p in sorted(portable.rglob("*")) if p.is_file())
    (portable / "manifest.sha256").write_text(manifest + "\n", "utf-8")
    print(portable)


if __name__ == "__main__":
    os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    main()
