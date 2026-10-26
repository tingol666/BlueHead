"""User-facing native audio failures; retain machine-readable recovery evidence."""
import asyncio
import logging


class Retirement:
    """Try every owned cleanup, retaining the first failure and original cause."""
    def __init__(self):
        self.first = None

    def failed(self, error):
        self.first = self.first or error
        logging.getLogger(__name__).warning('Resource cleanup failed: %s', error)

    def call(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (Exception, asyncio.CancelledError) as error:
            self.failed(error)

    async def async_call(self, function, *args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (Exception, asyncio.CancelledError) as error:
            self.failed(error)

    def finish(self, original=None):
        if self.first is not None and original is None:
            raise self.first


class SourceExpired(OSError):
    """An expected lifecycle boundary, distinct from a broken native channel."""


class NativeAudioError(OSError):
    def __init__(self, result):
        self.category = result.get("category", "audio_internal")
        self.native_code = result.get("native_code", 0)
        self.retryable = bool(result.get("retryable", False))
        self.detail = result.get("error", "")
        direction = {"output": "播放设备", "input": "麦克风", "system": "电脑声音采集"}.get(
            result.get("direction"), "音频设备")
        reasons = {
            "audio_endpoint": "暂不可用",
            "audio_busy": "正被占用",
            "audio_permission": "访问被拒绝，请检查 Windows 音频／麦克风权限",
            "audio_format": "不支持所需音频格式或采集方式",
            "audio_memory": "可用系统资源不足",
            "audio_timeout": "响应超时",
            "audio_internal": "未能完成操作",
        }
        reason = reasons.get(self.category, "未能完成操作")
        suffix = ("，后台将稍后重试" if self.retryable else
                  "，当前设备继续使用" if result.get("previous_route_active") else "，请检查后重试")
        super().__init__(direction + reason + suffix)


class AudioHealth:
    """Coalesce control-side notices; no polling or work on an audio callback."""
    def __init__(self):
        self.warned = {}

    def events(self, audio):
        states = audio.get("continuity", {}).get("devices", {}).get("recovery", {})
        if not states:
            self.warned.clear()
            return []
        events = []
        for key, direction, label in (("output", "output", "播放设备"),
                                      ("microphone", "input", "麦克风"),
                                      ("system", "system", "电脑声音采集")):
            value = states.get(key, {})
            state = value.get("state")
            if state == "needs_attention" or (state == "retrying" and value.get("attempts", 0) >= 2):
                signature = (value.get("category"), value.get("native_code"), state)
                if self.warned.get(key) != signature:
                    message = str(NativeAudioError({**value, "direction": direction,
                                                    "retryable": value.get("automatic", False)}))
                    events.append(dict(direction=key, state="warning", message=message))
                    self.warned[key] = signature
            elif state == "healthy" and key in self.warned:
                events.append(dict(direction=key, state="restored", message=label+"已恢复"))
                self.warned.pop(key)
        return events
