"""Owned private RPC connection. Control messages only, never PCM."""
import contextlib
import json
import os
from pathlib import Path
import struct
import subprocess
import threading
import time
import uuid
from .. import __version__
from ..errors import NativeAudioError, SourceExpired
MAX_REPLY = 1048576
_client = None
_client_lock = threading.Lock()

class NativeClient:
    def __init__(self, pipe=None, *, spawn=False, executable=None, assets=None, offline=True):
        import _winapi
        self.handle = None
        self.process = None
        self.lock = threading.RLock()
        self.closed = False
        self.pipe = pipe or rf"\\.\pipe\BlueHead-RT-{uuid.uuid4().hex}"
        assets = Path(assets or Path(__file__).resolve().parents[1] / "assets").resolve()
        if spawn:
            executable = Path(executable or assets / "BlueHead.Runtime.exe")
            self.process = subprocess.Popen(
                [str(executable), "--rpc", self.pipe, "--assets", str(assets), "--parent-pid", str(os.getpid()),
                 *(["--offline-test"] if offline else [])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        deadline = time.monotonic() + 5
        try:
            while True:
                try:
                    self.handle = _winapi.CreateFile(self.pipe, 0xC0000000, 0, 0, 3, 0x40000000, 0)
                    break
                except OSError:
                    if time.monotonic() >= deadline or (self.process and self.process.poll() is not None):
                        raise OSError("原生音频后台未能连接") from None
                    time.sleep(.01)
            hello = self.request("hello")
            if hello.get("version") != __version__ or hello.get("abi") != 1:
                raise OSError("原生音频后台版本不匹配，请完整更新程序")
        except BaseException:
            self.close()
            raise

    def _io(self, data=None, length=0, timeout=10):
        import _winapi
        deadline = time.monotonic() + timeout
        if data is not None:
            operation, error = _winapi.WriteFile(self.handle, data, overlapped=True)
        else:
            operation, error = _winapi.ReadFile(self.handle, length, overlapped=True)
        try:
            if error == 997:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if _winapi.WaitForMultipleObjects([operation.event], False, remaining) != 0:
                    operation.cancel()
                    operation.GetOverlappedResult(True)
                    raise TimeoutError("原生控制请求超时，音频线程与控制请求独立运行")
            count, error = operation.GetOverlappedResult(True)
            if error or count <= 0:
                raise OSError("原生音频后台连接已关闭")
            return count if data is not None else bytes(operation.getbuffer()[:count])
        finally:
            # GetOverlappedResult above retires every submitted operation before
            # its Python-owned allocation is released.
            pass

    def _read(self, length):
        result = bytearray()
        while len(result) < length:
            result.extend(self._io(length=length - len(result)))
        return bytes(result)

    def request(self, op, **values):
        with self.lock:
            if self.closed or self.handle is None:
                raise OSError("原生音频后台已关闭")
            payload = json.dumps({"op": op, **values}, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(payload) > 131072:
                raise ValueError("原生控制请求过长")
            packet = struct.pack("<I", len(payload)) + payload
            try:
                offset = 0
                while offset < len(packet):
                    offset += self._io(data=packet[offset:])
                length, = struct.unpack("<I", self._read(4))
                if not 0 < length <= MAX_REPLY:
                    raise OSError("原生控制响应无效")
                response = json.loads(self._read(length))
            except (OSError, ValueError):
                # A timed-out/partial exchange cannot safely be reused: a late
                # response could otherwise be mistaken for the next request.
                import _winapi
                _winapi.CloseHandle(self.handle)
                self.handle = None
                raise
            if not response.get("ok"):
                if response.get("category") == "source_expired":
                    raise SourceExpired(response.get("error", "Audio source session has expired"))
                if response.get("category"):
                    raise NativeAudioError(response)
                raise OSError(response.get("error", "原生音频操作失败"))
            return response.get("result")

    def wait_audio(self, ticket, timeout=8):
        deadline = time.monotonic() + timeout
        while True:
            result = self.request("audio_result", ticket=ticket)
            if result.get("done"):
                if not result.get("success"):
                    if result.get("category"):
                        raise NativeAudioError(result)
                    raise OSError(result.get("error", "音频设备操作已取消"))
                return result
            if time.monotonic() >= deadline:
                if not self.request("audio_cancel", ticket=ticket):
                    result = self.request("audio_result", ticket=ticket)
                    if result.get("success"):
                        return result
                raise TimeoutError("音频设备准备超时，已取消本次切换")
            time.sleep(.01)

    def audio(self, direction, endpoint_id=None, start=True, **values):
        ticket = self.request("audio_switch", direction=direction, endpoint_id=endpoint_id or "", start=start, **values)
        return self.wait_audio(ticket)

    def retry_audio(self, timeout=8):
        """Explicit rescan; the native owner keeps newer route/stop intent."""
        deadline = time.monotonic() + timeout
        errors = []
        for ticket in self.request("audio_retry"):
            if not ticket:
                continue
            try:
                self.wait_audio(ticket, timeout=max(0, deadline-time.monotonic()))
            except OSError as error:
                errors.append(str(error))
        return errors

    def close(self):
        import _winapi
        with self.lock:
            if self.closed:
                return
            if self.process and self.handle is not None:
                with contextlib.suppress(OSError, TimeoutError):
                    self.request("runtime_exit")
            self.closed = True
            if self.handle is not None:
                _winapi.CloseHandle(self.handle)
                self.handle = None
            if self.process:
                try:
                    self.process.wait(5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(2)
                if self.process.stderr:
                    self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def current():
    global _client
    pipe = os.environ.get("BLUEHEAD_NATIVE_PIPE")
    if not pipe:
        return None
    with _client_lock:
        if _client is None:
            _client = NativeClient(pipe)
        return _client
