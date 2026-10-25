"""Experimental LDAC remains isolated; its complete media path is native.

The pinned MIT decoder is not Sony-certified. Production code transports bytes
only: RTP extraction, decoder state and SpeexDSP SRC live in the child process.
NumPy is imported only by explicitly requested development array facades.
"""
import ctypes as c
import multiprocessing as mp
import os
from pathlib import Path
import struct
import time

MAX_PACKET = 65535
QUEUE_FRAMES = 48064
MAX_PCM = QUEUE_FRAMES * 8
STATS = struct.Struct("<10Q")
_FEED, _RESET, _STOP = b"F", b"R", b"X"


def _overlapped_result(operation, error, deadline):
    """The same Windows overlapped pipe mechanism as multiprocessing, bounded."""
    import _winapi
    if error == _winapi.ERROR_IO_PENDING:
        milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
        result = _winapi.WaitForMultipleObjects([operation.event], False, milliseconds)
        if result != _winapi.WAIT_OBJECT_0:
            operation.cancel()
            operation.GetOverlappedResult(True)
            raise TimeoutError("LDAC worker stalled")
    return operation.GetOverlappedResult(True)


def _send_deadline(pipe, message, deadline):
    if os.name != "nt":
        pipe.send_bytes(message)
        return
    import _winapi
    operation, error = _winapi.WriteFile(pipe.fileno(), message, overlapped=True)
    count, error = _overlapped_result(operation, error, deadline)
    if error or count != len(message):
        raise OSError("LDAC worker pipe write failed")


def _receive_deadline(pipe, maximum, deadline):
    if os.name != "nt":
        if not pipe.poll(max(0., deadline - time.monotonic())):
            raise TimeoutError("LDAC worker stalled")
        return pipe.recv_bytes(maximum)
    import _winapi
    operation, error = _winapi.ReadFile(pipe.fileno(), maximum, overlapped=True)
    count, error = _overlapped_result(operation, error, deadline)
    if error:
        raise OSError("LDAC worker pipe read failed or exceeded its bound")
    return operation.getbuffer()[:count]


class NativeLdac:
    """Raw-frame bridge for development; release RTP uses IsolatedMedia."""
    def __init__(self):
        self.handle = None
        path = Path(__file__).resolve().parent / "assets" / "bluehead_ldac.dll"
        self.library = c.CDLL(str(path))
        self.library.bh_ldac_create.restype = c.c_void_p
        self.library.bh_ldac_free.argtypes = [c.c_void_p]
        self.library.bh_ldac_decode.argtypes = [c.c_void_p, c.c_void_p, c.c_int, c.c_void_p, c.c_int]
        self.handle = self.library.bh_ldac_create()
        if not self.handle:
            raise MemoryError("LDAC allocation failed")

    def decode_bytes(self, frame):
        if not isinstance(frame, bytes) or not 4 <= len(frame) <= 515:
            raise ValueError("Invalid LDAC frame length")
        output = (c.c_float * 512)()
        count = self.library.bh_ldac_decode(self.handle, frame, len(frame), output, 512)
        if count not in (128, 256):
            raise ValueError("LDAC rejected a damaged/unsupported frame")
        return c.string_at(output, count * 8)

    def decode(self, frame):
        import numpy as np
        return np.frombuffer(self.decode_bytes(frame), dtype=np.float32).reshape(-1, 2).copy()

    def close(self):
        if self.handle:
            self.library.bh_ldac_free(self.handle)
            self.handle = None


class _NativeMedia:
    def __init__(self, rate, output_rate):
        self.handle = None
        assets = Path(__file__).resolve().parent / "assets"
        self.directory = os.add_dll_directory(str(assets))
        try:
            self.api = c.CDLL(str(assets / "bluehead_ldac_media.dll"))
            api = self.api
            api.bh_ldac_media_create.argtypes = [c.c_int] * 3
            api.bh_ldac_media_create.restype = c.c_void_p
            for name in ("destroy", "restart", "discard"):
                getattr(api, "bh_ldac_media_" + name).argtypes = [c.c_void_p]
            api.bh_ldac_media_feed.argtypes = [c.c_void_p, c.c_void_p, c.c_uint]
            api.bh_ldac_media_read.argtypes = [c.c_void_p, c.c_void_p, c.c_uint]
            api.bh_ldac_media_read.restype = c.c_uint
            api.bh_ldac_media_value.argtypes = [c.c_void_p, c.c_int]
            api.bh_ldac_media_value.restype = c.c_uint64
            self.handle = api.bh_ldac_media_create(rate, 2, output_rate)
            if not self.handle:
                raise OSError("Native LDAC media engine could not initialize")
        except BaseException:
            self.directory.close()
            raise

    def decode_packet(self, packet):
        return self.api.bh_ldac_media_feed(self.handle, packet, len(packet))

    def restart(self):
        if self.api.bh_ldac_media_restart(self.handle) < 0:
            raise OSError("Native LDAC reset failed")

    def values(self):
        return [self.api.bh_ldac_media_value(self.handle, index) for index in range(10)]

    def read_pcm(self):
        frames = self.api.bh_ldac_media_value(self.handle, 7)
        if frames > QUEUE_FRAMES:
            raise ValueError("Native LDAC queue exceeds its bound")
        output = (c.c_float * (frames * 2))()
        frames = self.api.bh_ldac_media_read(self.handle, output, frames)
        return c.string_at(output, frames * 8)

    def close(self):
        if self.handle:
            self.api.bh_ldac_media_destroy(self.handle)
            self.handle = None
        if self.directory:
            self.directory.close()
            self.directory = None


def _worker(pipe, mode="raw", rate=48000, output_rate=48000):
    decoder = None
    try:
        decoder = _NativeMedia(rate, output_rate) if mode == "media" else NativeLdac()
        pipe.send_bytes(b"READY")
        while True:
            message = pipe.recv_bytes(MAX_PACKET + 1)
            if not message or message == _STOP:
                break
            if message == _RESET:
                if mode == "media":
                    decoder.restart()
                    pipe.send_bytes(b"\x00" + STATS.pack(*decoder.values()))
                else:
                    decoder.close()
                    decoder = NativeLdac()
                    pipe.send_bytes(b"READY")
                continue
            if message[:1] != _FEED:
                break
            packet = message[1:]
            if mode == "media":
                result = decoder.decode_packet(packet)
                if result == -3:
                    raise OSError("LDAC decoder could not recover")
                values = decoder.values()
                pcm = decoder.read_pcm()
                pipe.send_bytes(bytes([int(result < 0)]) + STATS.pack(*values) + pcm)
            else:
                try:
                    pipe.send_bytes(b"\x00" + decoder.decode_bytes(packet))
                except ValueError:
                    pipe.send_bytes(b"\x01")
                    decoder.close()
                    decoder = NativeLdac()
    except (EOFError, OSError):
        pass
    finally:
        if decoder:
            decoder.close()
        pipe.close()


class _ProcessOwner:
    def _start_process(self, mode, rate, output_rate=48000):
        context = mp.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child, mode, rate, output_rate), daemon=True)
        try:
            self.process.start()
        except BaseException:
            self.pipe.close()
            child.close()
            self.process.close()
            self.process = None
            raise
        child.close()
        try:
            if not self.pipe.poll(8) or self.pipe.recv_bytes(16) != b"READY":
                raise RuntimeError("LDAC worker could not start")
        except (EOFError, OSError) as error:
            self._dispose_process()
            raise RuntimeError("LDAC worker could not start") from error
        except BaseException:
            self._dispose_process()
            raise

    def _dispose_process(self):
        process = getattr(self, "process", None)
        if process is None:
            return
        try:
            _send_deadline(self.pipe, _STOP, time.monotonic() + .05)
        except (BrokenPipeError, EOFError, OSError, TimeoutError):
            pass
        process.join(.3)
        if process.is_alive():
            process.terminate()
            process.join(1)
        self.pipe.close()
        if not process.is_alive():
            process.close()
        self.process = None

    def _exchange(self, message, maximum):
        deadline = time.monotonic() + .5
        _send_deadline(self.pipe, message, deadline)
        return _receive_deadline(self.pipe, maximum, deadline)


class LdacDecoder(_ProcessOwner):
    """Legacy raw-frame development facade, retaining process isolation."""
    def __init__(self, rate):
        self.rate, self.process = rate, None
        self._start_process("raw", rate)

    def reset(self):
        try:
            if self._exchange(_RESET, 16) != b"READY":
                raise RuntimeError("LDAC reset failed")
        except (TimeoutError, EOFError, OSError):
            self.close()
            raise

    def decode_bytes(self, frame):
        if not isinstance(frame, bytes) or not 4 <= len(frame) <= 515:
            raise ValueError("Invalid LDAC frame length")
        try:
            data = self._exchange(_FEED + frame, 4097)
        except (TimeoutError, EOFError, OSError):
            self.close()
            raise
        if not data or data[0] or len(data[1:]) not in (1024, 2048):
            raise ValueError("LDAC decoder rejected a damaged/unsupported frame")
        return data[1:]

    def decode(self, frame):
        import numpy as np
        return np.frombuffer(self.decode_bytes(frame), dtype=np.float32).reshape(-1, 2).copy()

    def close(self):
        self._dispose_process()


class IsolatedMedia(_ProcessOwner):
    """NativeMedia-compatible LDAC pipeline; PCM crosses IPC as float bytes.

    One decoder worker/thread owns this object. After a worker crash or timeout,
    the faulting packet is dropped and the next packet creates a fresh worker.
    Normal pause/resume resets native state in the existing process.
    """
    def __init__(self, fmt, output_rate=48000):
        if fmt.codec != "ldac" or fmt.channels != 2 or fmt.rate not in (44100, 48000, 88200, 96000):
            raise ValueError("Unsupported LDAC format")
        if not isinstance(output_rate, int) or not 8000 <= output_rate <= 192000:
            raise ValueError("Unsupported LDAC output rate")
        self.fmt, self.output_rate = fmt, output_rate
        self.process = None
        self.closed = False
        self._values = [0] * 10
        self._base = [0] * 10
        self._pcm = bytearray()
        self._failed = False
        self._ensure_process()

    def _ensure_process(self):
        if self.closed:
            raise RuntimeError("LDAC media is closed")
        if self.process is not None:
            return
        if self._failed:
            self._values[4] += 1
        self._base = self._values.copy()
        self._start_process("media", self.fmt.rate, self.output_rate)
        self._failed = False

    def _failed_process(self):
        self._dispose_process()
        self._pcm.clear()
        self._values[3] += 1
        self._values[6] = 1
        self._failed = True

    def _request(self, message):
        self._ensure_process()
        try:
            data = self._exchange(message, MAX_PCM + STATS.size + 1)
            if len(data) < 1 + STATS.size or (len(data) - STATS.size - 1) % 8 or data[0] > 1:
                raise OSError("Invalid LDAC worker response")
            values = STATS.unpack_from(data, 1)
            pcm = data[1 + STATS.size:]
            if len(pcm) > MAX_PCM:
                raise OSError("LDAC output exceeds its bound")
            for field in range(10):
                self._values[field] = values[field] if field in (6, 7) else self._base[field] + values[field]
            self._pcm.extend(pcm)
            if len(self._pcm) > MAX_PCM:
                skip = len(self._pcm) - MAX_PCM
                del self._pcm[:skip]
                self._values[8] += skip // 8
                self._base[8] += skip // 8
            return data[0]
        except (TimeoutError, EOFError, OSError) as error:
            self._failed_process()
            if isinstance(error, TimeoutError):
                raise
            raise RuntimeError("LDAC worker stopped; the next packet will recover") from error

    def value(self, field):
        return len(self._pcm) // 8 if field == 7 else self._values[field] if 0 <= field < 10 else 0

    frames = property(lambda self: self.value(0))
    bytes = property(lambda self: self.value(1))
    coded_bytes = property(lambda self: self.value(2))
    errors = property(lambda self: self.value(3))
    recoveries = property(lambda self: self.value(4))
    lost = property(lambda self: self.value(5))
    synchronizing = property(lambda self: bool(self.value(6)))
    depacketizer = property(lambda self: self)

    def decode_packet(self, data):
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_PACKET:
            raise ValueError("Invalid media packet")
        if self._request(_FEED + data):
            from .protocol import PacketError
            raise PacketError("Native LDAC rejected a damaged packet")

    def restart(self):
        self._pcm.clear()
        if self._request(_RESET):
            raise OSError("Native LDAC reset failed")

    def discard_pcm(self):
        self._pcm.clear()

    def read_pcm(self):
        pcm = bytes(self._pcm)
        self._pcm.clear()
        return pcm

    def write_to(self, mixer, key):
        pcm = self.read_pcm()
        if not pcm:
            return 0
        result = mixer.write_float_bytes(key, pcm)
        return len(pcm) // 8 if result is None else result

    def feed(self, data):
        """Development array facade; release processing never calls this method."""
        self.decode_packet(data)
        pcm = self.read_pcm()
        if not pcm:
            return []
        import numpy as np
        return [np.frombuffer(pcm, dtype=np.float32).reshape(-1, 2).copy()]

    def close(self):
        self.closed = True
        self._pcm.clear()
        self._dispose_process()

    def __del__(self):
        if getattr(self, "process", None) is not None:
            self.close()
