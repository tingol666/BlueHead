"""Optional shared Windows A2DP receiver; codec choice remains with Windows."""
import asyncio
import contextlib


class NativeReceiver:
    def __init__(self, emit):
        self.emit = emit
        self.connections = {}

    async def run(self, stop):
        import winrt.runtime
        import winrt.windows.foundation
        import winrt.windows.foundation.collections
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.media.audio import AudioPlaybackConnection
        winrt.runtime.init_apartment(winrt.runtime.ApartmentType.MULTI_THREADED)
        try:
            selector = AudioPlaybackConnection.get_device_selector()
            self.emit("status", message="基础模式正在接收。请在手机／平板播放音乐或视频；尚未配对时，在电脑打开 Windows 蓝牙设置，添加并配对手机／平板。")
            while not stop.is_set():
                devices = await DeviceInformation.find_all_async(selector, [])
                for info in devices:
                    if info.id in self.connections:
                        continue
                    connection = AudioPlaybackConnection.try_create_from_id(info.id)
                    if connection:
                        try:
                            await connection.start_async()
                            result = await connection.open_async()
                            self.connections[info.id] = connection
                            self.emit("device", key=info.name, state=str(result.status))
                        except Exception:
                            connection.close()
                            raise
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            for connection in self.connections.values():
                with contextlib.suppress(Exception):
                    connection.close()
            self.connections.clear()
            winrt.runtime.uninit_apartment()
