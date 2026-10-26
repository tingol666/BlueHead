import ctypes as C
import time

import numpy as np

from bluehead.audio import Mixer
from .offline_audio import OfflineEndpoints


def test_callback_arrival_gaps_are_separate_from_processing_time_and_restart(monkeypatch):
    endpoints = OfflineEndpoints(monkeypatch)
    mixer = Mixer(buffer_ms=0)
    mixer.add('source')
    mixer.write('source', np.full((4800, 2), .1, 'f4'))
    raw = endpoints.create_output(mixer)
    api = endpoints.owner.api
    api.bh_device_timing.argtypes = [C.c_void_p, C.c_int]
    api.bh_device_timing.restype = C.c_uint64
    try:
        raw.start()
        output = np.empty((480, 2), 'f4')
        endpoints.process(raw, output=output)
        assert api.bh_device_timing(raw.handle, 2) == 0
        time.sleep(.04)  # An explicit harness arrival stall, not an audio-device claim.
        endpoints.process(raw, output=output)
        assert api.bh_device_timing(raw.handle, 0) >= 30000
        assert api.bh_device_timing(raw.handle, 1) >= 20000
        assert api.bh_device_timing(raw.handle, 2) == 1
        raw.stop()
        time.sleep(.04)
        raw.start()
        endpoints.process(raw, output=output)
        assert api.bh_device_timing(raw.handle, 2) == 1
    finally:
        raw.close()
        endpoints.close()
