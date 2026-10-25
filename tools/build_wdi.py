"""Build LGPL libwdi catalog preparation for Windows 10+ inbox WinUSB.

The original source remains in vendor/libwdi. Patches are applied to a build
copy. We do not embed/repackage Microsoft coinstallers or any kernel binaries.
The exact-instance binding and rollback are implemented in BlueHead.Platform.
"""
import ctypes
import json
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
build = root / "work/build-wdi"
source = root / "vendor/libwdi/libwdi"
build.mkdir(parents=True, exist_ok=True)
for path in source.iterdir():
    if path.is_file():
        shutil.copy2(path, build / path.name)
(build / "config.h").write_text('''#pragma once
#define OPT_M64 1
#define WDK_DIR "."
#define WDF_VER 1011
#define ENABLE_LOGGING 1
#define LIBWDI_DLL_EXPORT 1
#define PACKAGE_VERSION "1.5.1-bluehead-modern"
''', "utf-8")
original = (build / "libwdi.c").read_text("utf-8")
needle = 'int get_version_info(int driver_type, VS_FIXEDFILEINFO* driver_info)\n{'
assert needle in original
replacement = needle + '''
    /* BlueHead: WinUSB is supplied by Windows. This is the version/date of
     * our INF package, not a repackaged Microsoft driver binary. */
    if (driver_type == WDI_WINUSB && driver_info != NULL) {
        FILETIME date;
        GetSystemTimeAsFileTime(&date);
        memset(driver_info, 0, sizeof(*driver_info));
        driver_info->dwSignature = 0xFEEF04BD;
        driver_info->dwFileVersionMS = 0x00010000;
        driver_info->dwFileDateLS = date.dwLowDateTime;
        driver_info->dwFileDateMS = date.dwHighDateTime;
        return WDI_SUCCESS;
    }
'''
(build / "libwdi.c").write_text(original.replace(needle, replacement), "utf-8")
resources = [
    ("winusb.inf.in", (root / "native/winusb-modern.inf.in").read_bytes()),
    ("winusb.cat.in", b"# Only the generated INF is catalogued; WinUSB is an inbox driver.\n"),
]
embedded = ["#include <stdint.h>", "#include <stddef.h>"]
for index, (_, data) in enumerate(resources):
    embedded.append(f"static const unsigned char file_{index}[] = {{" + ",".join(str(b) for b in data) + "};")
embedded.append("struct res { const char *subdir, *name; size_t size; int64_t creation_time; const unsigned char *data; };")
embedded.append("const struct res resource[] = {")
for index, (name, data) in enumerate(resources):
    embedded.append(f'{{"", "{name}", {len(data)}, 1704067200, file_{index}}},')
embedded.extend(["};", "const int nb_resources = sizeof(resource)/sizeof(resource[0]);"])
(build / "embedded.h").write_text("\n".join(embedded) + "\n", "utf-8")
gcc = shutil.which("gcc") or "D:/Code/MSYS2/ucrt64/bin/gcc.exe"
output = root / "bluehead/assets/libwdi.dll"
subprocess.run([
    gcc, "-shared", "-std=gnu11", "-O2", "-static-libgcc", "-Wl,--no-insert-timestamp",
    "-DWINVER=0x0601", "-D_WIN32_WINNT=0x0601", "-D_WIN32_IE=0x0700", "-DCOBJMACROS",
    "-DLIBWDI_DLL_EXPORT", "-I" + str(build), "-o", str(output),
    *[str(build / name) for name in ("logging.c", "tokenizer.c", "vid_data.c", "pki.c", "libwdi_dlg.c", "libwdi.c")],
    "-lsetupapi", "-lole32", "-lntdll", "-luuid", "-ladvapi32", "-lcomctl32", "-lshell32", "-luser32", "-lgdi32",
], check=True)
library = ctypes.WinDLL(str(output))
library.wdi_is_driver_supported.argtypes = [ctypes.c_int, ctypes.c_void_p]
library.wdi_is_driver_supported.restype = ctypes.c_int
assert library.wdi_is_driver_supported(0, None)
print(json.dumps({"libwdi": "1.5.1-bluehead-modern", "inbox_winusb": True, "bytes": output.stat().st_size}))
