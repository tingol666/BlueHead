"""Regenerate deterministic, synthetic release self-check packets (development only)."""
import base64
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from tests.vectors import signal, encode, wire_packets, LdacEncoder, rtp  # noqa: E402

records = []
for codec in ("sbc", "aac", "aptx", "aptx_hd", "ldac"):
    pcm = signal(48000, .12)
    if codec == "ldac":
        encoder = LdacEncoder(48000, 1)
        try:
            packets, position = [], 0
            for sequence, packet in enumerate(encoder.encode(pcm)):
                packets.append(rtp(packet, sequence, position))
                position += (packet[0] & 31) * 128
        finally:
            encoder.close()
    else:
        packets = list(wire_packets(codec, encode(codec, pcm, 48000), 48000))
    records.append({"codec": codec, "rate": 48000, "channels": 2,
                    "sha256": hashlib.sha256(b"".join(packets)).hexdigest(),
                    "packets": [base64.b64encode(packet).decode("ascii") for packet in packets]})
(root / "bluehead/assets/selfcheck_vectors.json").write_text(json.dumps({
    "scope": "Deterministic generated sine tones; no recorded or copyrighted audio",
    "generator": "tools/generate_audio_vectors.py with tests/vectors.py",
    "vectors": records,
}, separators=(",", ":")) + "\n", "utf-8", newline="\n")
