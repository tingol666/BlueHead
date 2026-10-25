"""Build the small versioned, shell-free Windows application entry point."""
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
numeric = ','.join(str(int(part)) for part in version.split('.')) + ',0'
build = root / 'work/launcher-build'
build.mkdir(parents=True, exist_ok=True)
gcc = Path(shutil.which('gcc') or 'D:/Code/MSYS2/ucrt64/bin/gcc.exe')
windres = gcc.parent / 'windres.exe'
icon = (root / 'bluehead/assets/bluehead.ico').as_posix()
(build / 'launcher.rc').write_text(f'''1 ICON "{icon}"
1 VERSIONINFO
FILEVERSION {numeric}
PRODUCTVERSION {numeric}
FILEOS 0x40004
FILETYPE 1
BEGIN
 BLOCK "StringFileInfo"
 BEGIN
  BLOCK "040904B0"
  BEGIN
   VALUE "FileDescription", "BlueHead"
   VALUE "ProductName", "BlueHead"
   VALUE "FileVersion", "{version}"
   VALUE "LegalCopyright", "BlueHead contributors"
  END
 END
 BLOCK "VarFileInfo"
 BEGIN
  VALUE "Translation", 0x409, 1200
 END
END
''', 'utf-8')
subprocess.run([str(windres), str(build / 'launcher.rc'), str(build / 'launcher.o')], check=True)
output = Path(sys.argv[1]).resolve()
output.parent.mkdir(parents=True, exist_ok=True)
subprocess.run([str(gcc), '-municode', '-mwindows', '-Os', '-static-libgcc', '-Wl,--no-insert-timestamp',
                '-o', str(output), str(root / 'native/launcher.c'), str(build / 'launcher.o')], check=True)
