import threading
import time

import numpy as np

from bluehead.audio import Mixer
from bluehead.backend import MediaSession
from bluehead.protocol import Format


def test_late_decoder_result_cannot_play_after_suspend_and_resume_works(monkeypatch):
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
    class SlowDecoder:
        def __init__(self, *args):
            pass
        def feed(self, data):
            if data == b'old':
                entered.set()
                assert release.wait(2)
            delivered.set()
            return [np.full((480, 2), .1, np.float32)]
        def close(self):
            pass
    monkeypatch.setattr('bluehead.backend.Decoder', SlowDecoder)
    mixer = Mixer(buffer_ms=0)
    session = MediaSession('phone', mixer, lambda *args, **kwargs: None)
    try:
        session.configure(Format('sbc', 48000, 2))
        session.push(b'old')
        assert entered.wait(2)
        session.pause()
        release.set()
        assert delivered.wait(2)
        time.sleep(.01)
        assert mixer.inputs['phone'].frames == 0
        delivered.clear()
        session.push(b'paused packet')
        assert not delivered.wait(.03)
        session.resume()
        session.push(b'new')
        assert delivered.wait(2)
        for _ in range(100):
            if mixer.inputs['phone'].frames:
                break
            time.sleep(.001)
        assert mixer.inputs['phone'].frames == 480
    finally:
        release.set()
        session.close()
