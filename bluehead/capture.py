"""Drain the native microphone queue on a daemon worker; no Python DSP."""
import threading

from .audio_device import context


class CaptureStream:
    def __init__(self, device, rate, consume, emit):
        self.device = device
        self.native = context().create_capture(device, output_rate=rate)
        self.consume, self.emit = consume, emit
        self.stop_event = threading.Event()
        self.worker = None
        self.closed = False

    @property
    def endpoint_id(self):
        return self.native.endpoint_id

    @property
    def active(self):
        return self.native.active and self.worker is not None and self.worker.is_alive()

    def start(self):
        if self.closed:
            raise RuntimeError("麦克风已经关闭")
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.native.start()
        self.worker = threading.Thread(target=self.run, daemon=True, name="native-microphone-drain")
        try:
            self.worker.start()
        except BaseException:
            self.worker = None  # A Thread that never started cannot be joined.
            self.native.stop()
            raise

    def run(self):
        try:
            while not self.stop_event.is_set():
                data = self.native.read(1024)
                if data:
                    self.consume(data)
                else:
                    self.stop_event.wait(.003)
        except Exception as error:
            if not self.stop_event.is_set():
                self.emit("warning", message=f"麦克风采集已停止，将重新连接音频设备：{error}")

    def stop(self):
        self.stop_event.set()
        self.native.stop()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(2)
            if self.worker.is_alive():
                raise OSError("麦克风采集线程仍在退出，将稍后重试")

    def close(self):
        if not self.closed:
            self.stop()
            self.native.close()
            self.closed = True

    def snapshot(self):
        return self.native.snapshot()
