"""Native Core Audio identity snapshots, independent of PortAudio's cached list."""
import ctypes as c
import uuid

from .windows_audio import audio_apartment
from .audio_runtime import on_audio_thread


class Guid(c.Structure):
    _fields_ = [("value", c.c_ubyte * 16)]

    @classmethod
    def of(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


def call(pointer, slot, types, *args):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    function = c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(table[slot])
    return function(pointer, *args)


def check(result):
    if result < 0:
        raise OSError(f"Core Audio HRESULT 0x{result & 0xffffffff:08X}")


def release(pointer):
    if pointer:
        call(pointer, 2, [])


def identifier(pointer):
    text = c.c_void_p()
    check(call(pointer, 5, [c.POINTER(c.c_void_p)], c.byref(text)))
    try:
        return c.wstring_at(text)
    finally:
        ole = c.WinDLL("ole32")
        ole.CoTaskMemFree.argtypes = [c.c_void_p]
        ole.CoTaskMemFree(text)


def snapshot():
    with audio_apartment():
        ole = c.WinDLL("ole32")
        cls = Guid.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
        iid = Guid.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
        enumerator, collection = c.c_void_p(), c.c_void_p()
        ole.CoCreateInstance.argtypes = [c.c_void_p, c.c_void_p, c.c_uint, c.c_void_p, c.c_void_p]
        ole.CoCreateInstance.restype = c.c_long
        check(ole.CoCreateInstance(c.byref(cls), None, 1, c.byref(iid), c.byref(enumerator)))
        try:
            check(call(enumerator, 3, [c.c_int, c.c_uint, c.POINTER(c.c_void_p)], 2, 1, c.byref(collection)))
            count = c.c_uint()
            check(call(collection, 3, [c.POINTER(c.c_uint)], c.byref(count)))
            devices = []
            for index in range(count.value):
                device = c.c_void_p()
                check(call(collection, 4, [c.c_uint, c.POINTER(c.c_void_p)], index, c.byref(device)))
                try:
                    devices.append(identifier(device))
                finally:
                    release(device)
            defaults = []
            for direction in (0, 1):
                for role in (0, 1, 2):
                    device = c.c_void_p()
                    result = call(enumerator, 4, [c.c_int, c.c_int, c.POINTER(c.c_void_p)], direction, role, c.byref(device))
                    try:
                        defaults.append(identifier(device) if result >= 0 else None)
                    finally:
                        release(device)
            return tuple(sorted(devices)), tuple(defaults)
        finally:
            release(collection)
            release(enumerator)


@on_audio_thread
def portaudio_id(index):
    if index is None:
        return None
    import sounddevice as sd
    library = c.CDLL(sd._libname)
    function = library.PaWasapi_GetIMMDevice
    function.argtypes = [c.c_int, c.POINTER(c.c_void_p)]
    function.restype = c.c_int
    pointer = c.c_void_p()
    if function(int(index), c.byref(pointer)) < 0 or not pointer:
        return None
    # PortAudio owns this reference. GetId returns a separately allocated string.
    return identifier(pointer)


@on_audio_thread
def find_endpoint(identity, direction):
    import sounddevice as sd
    apis = sd.query_hostapis()
    for index, info in enumerate(sd.query_devices()):
        if (info["max_" + direction + "_channels"] and "WASAPI" in apis[info["hostapi"]]["name"]
                and portaudio_id(index) == identity):
            return index
    raise ValueError("所选音频设备已断开，请重新选择")
