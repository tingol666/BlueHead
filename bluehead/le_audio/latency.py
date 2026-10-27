"""LE presentation budget, including endpoint buffering and the existing SRC.

Control-plane calculations only. Like mature BAP renderers, the requested delay
is shared by the decoder queue and downstream endpoint; it is not added ON TOP
of the application's ordinary media buffering. No new SRC or signal processing.
"""

from dataclasses import dataclass
import math
from bumble.profiles.ascs import AseReasonCode as Reason
from .configuration import ConfigurationError


@dataclass(frozen=True)
class Plan:
    presentation_us: int
    queue_us: int
    endpoint_us: int
    minimum_us: int


class PresentationBudget:
    def __init__(self, client):
        self.client = client

    def downstream(self, source=False):
        status = self.client.request("snapshot")
        devices = status.get("devices", {})
        endpoint = devices.get("input" if source else "output", {})
        period = endpoint.get("period_us", 10000)
        buffered = endpoint.get("buffer_us", 20000)
        for value in (period, buffered):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000000:
                raise ConfigurationError(
                    "Invalid native endpoint timing", Reason.PRESENTATION_DELAY, unsupported=True
                )
        # Closed/unopened endpoints report zero timing; do not advertise an
        # unrealistically short delay before capture/playback has been opened.
        period = period or 10000
        buffered = buffered or 2 * period
        rate = endpoint.get("output_rate", endpoint.get("sample_rate", 48000)) or 48000
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 768000:
            raise ConfigurationError("Invalid endpoint rate", Reason.PRESENTATION_DELAY, unsupported=True)
        filter_us = endpoint.get("filter_latency_frames", 72) * 1000000 / rate
        # One half callback quantum centres the existing clock controller.
        # Keep the existing 1.5ms transparent limiter/SRC lookahead in the budget.
        return int(math.ceil(buffered + period / 2 + max(1500, filter_us)))

    def capabilities(self, source, codec):
        minimum = ((self.downstream(source) + codec.interval_us + 999) // 1000) * 1000
        if minimum > 80000:
            raise ConfigurationError(
                "Audio endpoint cannot meet LE presentation delay", Reason.PRESENTATION_DELAY, unsupported=True
            )
        return minimum, 80000, max(minimum, 40000), max(minimum, 60000)

    def plan(self, codec, presentation_us, *, source=False, broadcast=False):
        downstream = self.downstream(source)
        minimum = downstream + codec.interval_us
        maximum = 180000 if broadcast else 80000
        if type(presentation_us) is not int or not minimum <= presentation_us <= maximum:
            raise ConfigurationError(
                "Requested presentation delay does not fit this audio endpoint",
                Reason.PRESENTATION_DELAY,
                unsupported=True,
            )
        return Plan(presentation_us, presentation_us - downstream, downstream, minimum)
