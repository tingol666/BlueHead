"""Build boundaries and failure-unwind contracts, without USB/audio hardware."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tools.native_build import INCLUDE_DIRS
from tools.sync_version import check

ROOT = Path(__file__).resolve().parents[1]


def dependencies(path, seen=None):
    seen = set() if seen is None else seen
    if path in seen:
        return seen
    seen.add(path)
    for name in re.findall(r'^#include "([^"]+)"', path.read_text('utf-8'), re.M):
        for directory in (path.parent, *INCLUDE_DIRS):
            target = (directory / name).resolve()
            if target.is_file():
                dependencies(target, seen)
                break
    return seen


def test_audio_graph_and_workers_cannot_reach_physical_usb_implementation():
    for entry in ('runtime/audio_graph.hpp', 'runtime/audio/media_worker.hpp',
                  'runtime/audio/endpoint_actor.hpp'):
        closure = dependencies(ROOT / 'native' / entry)
        assert not any(p.name in {'runtime_usb.hpp', 'libusb.h'} for p in closure)
    worker = dependencies(ROOT / 'native/runtime/audio/media_worker.hpp')
    assert not any(p.name in {'audio_graph.hpp', 'runtime_core.hpp', 'endpoint_actor.hpp'} for p in worker)


def test_stable_control_facade_shares_one_client_and_no_second_implementation():
    from bluehead import native_control, native_runtime
    from bluehead.native_control import client
    for name in native_runtime.__all__:
        assert getattr(native_runtime, name) is getattr(native_control, name)
    assert native_runtime.current is client.current
    assert native_runtime.NativeClient is client.NativeClient


def test_generated_language_versions_match_authoritative_project_version():
    check()


def test_native_host_missing_sidecar_exits_with_original_error(tmp_path):
    result = subprocess.run([str(ROOT / 'bluehead/assets/BlueHead.Runtime.exe'),
                             '--engine', '--offline-test', '--source-root', str(tmp_path / 'missing'),
                             '--assets', str(ROOT / 'bluehead/assets'), '--data-dir', str(tmp_path / 'data')],
                            input=b'', capture_output=True, timeout=15)
    assert result.returncode == 2
    assert b'Native runtime: Native child process could not start' in result.stderr


def test_native_owner_unwinds_partial_construction_before_library_release(tmp_path):
    compiler = shutil.which('g++') or 'D:/Code/MSYS2/ucrt64/bin/g++.exe'
    if not Path(compiler).is_file():
        pytest.skip('Native C++ toolchain is required for ownership contract')
    source = tmp_path / 'ownership.cpp'
    source.write_text(r'''
#include "runtime_resource.hpp"
#include <cassert>
#include <stdexcept>
static int freed = 0, library = 0;
static void release(void *value) { assert(library == 1); freed += *static_cast<int *>(value); }
struct Library { Library() { library = 1; } ~Library() { assert(freed == 3); library = 0; } };
struct Partial {
  Library api;
  bh::NativeResource first, second;
  Partial(int *a, int *b) { first.reset(a, release); second.reset(b, release); throw std::runtime_error("fail"); }
};
int main() {
  int a = 1, b = 2;
  try { Partial p(&a, &b); } catch (const std::runtime_error &) {}
  assert(freed == 3 && library == 0);
  bh::NativeResource empty;
  empty.reset(); empty.reset();
}
''', 'utf-8')
    executable = tmp_path / 'ownership.exe'
    subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-static',
                    '-I', str(ROOT / 'native/runtime'), str(source), '-o', str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=10)
