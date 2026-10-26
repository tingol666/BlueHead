"""Build the optional Windows native fault tracer from source."""
import subprocess
from native_build import ROOT, compiler

subprocess.run([compiler(), '-shared', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                '-static-libgcc', '-Wl,--no-insert-timestamp', '-s',
                str(ROOT / 'native/platform/windows/native_trace.c'),
                '-o', str(ROOT / 'bluehead/assets/bluehead_trace.dll'), '-luser32'], check=True)
