"""Independent PyAV mSBC test oracle, never shipped in the receiver."""
import av
import numpy as np

class Msbc:
    """HFP H2 framing, 120 mono samples per 7.5 ms frame."""
    SEQUENCES = (0x08, 0x38, 0xC8, 0xF8)

    def __init__(self):
        self.decoder = av.CodecContext.create("sbc", "r")
        self.decoder.sample_rate = 16000
        self.decoder.layout = "mono"
        self.decoder.open()
        self.encoder = av.CodecContext.create("sbc", "w")
        self.encoder.sample_rate = 16000
        self.encoder.layout = "mono"
        self.encoder.format = "s16"
        self.encoder.options = {"msbc": "1"}
        self.encoder.open()
        if self.encoder.frame_size != 120 or self.encoder.options:
            raise RuntimeError("FFmpeg mSBC encoder unavailable")
        self.receive_buffer = bytearray()
        self.sequence = 0

    def decode(self, data):
        self.receive_buffer.extend(data)
        if len(self.receive_buffer) > 512:
            del self.receive_buffer[:-120]
        result = []
        while len(self.receive_buffer) >= 60:
            b = self.receive_buffer
            if b[0] != 1 or b[1] not in self.SEQUENCES or b[2] != 0xAD:
                del b[0]
                continue
            packet = bytes(b[2:59])
            del b[:60]
            for frame in self.decoder.decode(av.Packet(packet)):
                result.append(frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.0)
        return result

    def encode(self, pcm):
        samples = np.frombuffer(pcm, dtype="<i2").reshape(1, 120)
        frame = av.AudioFrame.from_ndarray(samples, "s16", "mono")
        frame.sample_rate = 16000
        packets = self.encoder.encode(frame)
        if len(packets) != 1 or packets[0].size != 57:
            raise RuntimeError("Invalid mSBC frame length")
        header = bytes([1, self.SEQUENCES[self.sequence]])
        self.sequence = (self.sequence + 1) % 4
        return header + bytes(packets[0]) + b"\x00"


