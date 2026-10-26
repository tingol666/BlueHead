"""Opt-in bounded numeric audio counters, written only by Python control.

No samples, device identifiers, pairing keys, track metadata or HCI packets.
Disk I/O never runs on the native USB/audio threads. Disabled by default.
"""
import json
import math
import os
from pathlib import Path
import re
import threading
import time


def numeric(value, depth=0):
    if depth > 5:
        return None
    if type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [numeric(item, depth + 1) for item in value[:8]]
    if isinstance(value, dict):
        return {key: cleaned for key, item in list(value.items())[:64]
                if isinstance(key, str) and re.fullmatch('[a-z_]{1,48}', key)
                and (cleaned := numeric(item, depth + 1)) is not None}
    return None


class AudioCounters:
    def __init__(self, directory, *, clock=time.monotonic, maximum=2_000_000):
        self.path = Path(directory) / 'audio-counters.log'
        self.enabled = os.environ.get('BLUEHEAD_AUDIO_COUNTER_LOG') == '1'
        self.clock, self.maximum, self.last = clock, maximum, None
        self.lock = threading.Lock()

    def record(self, audio):
        if not self.enabled:
            return
        with self.lock:
            now = self.clock()
            if self.last is not None and now - self.last < 2:
                return
            raw = audio.get('continuity', {})
            data = {key: numeric(raw[key]) for key in (
                'quality', 'sources', 'devices', 'usb', 'voice', 'uplink',
                'bridge_errors', 'device_reopens', 'microphone_frames', 'system_frames',
            ) if key in raw}
            if not data:
                return
            text = json.dumps({'monotonic_seconds': now, **data}, allow_nan=False, separators=(',', ':')) + '\n'
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(text.encode('utf-8')) > self.maximum:
                    self.path.replace(self.path.with_suffix('.log.1'))
                with self.path.open('a', encoding='utf-8') as output:
                    output.write(text)
                self.last = now
            except OSError:
                self.enabled = False
