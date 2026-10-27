"""Project HFP 1.9 fixes layered on the pinned Bumble state machine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bumble import hfp, sdp


MAX_AT_BUFFER = 16 * 1024
MAX_AT_LINE = 4 * 1024
MAX_AT_QUEUE = 128
_UNSOLICITED_CODES = {"+BCS", "+CIEV", "+VGS", "+VGM", "RING", "+CLIP", "+BVRA"}
_PROTOCOL_ERRORS = {
    "buffer_limit": "HFP receive buffer limit exceeded",
    "line_limit": "HFP response line limit exceeded",
    "queue_limit": "HFP response queue limit exceeded",
    "command_limit": "HFP command response limit exceeded",
    "command_timeout": "HFP command timed out",
    "command_cancelled": "HFP command was interrupted",
    "invalid_response": "HFP response is malformed",
}


def _bounded_cind_test(response):
    """Limit the upstream SLC's expansion of advertised indicator ranges."""
    if len(response.parameters) > 32:
        return False
    try:
        for indicator in response.parameters:
            if len(indicator) != 2 or not isinstance(indicator[1], list) or len(indicator[1]) > 32:
                return False
            supported_count = 0
            for value in indicator[1]:
                parts = value.split(b"-")
                if len(parts) not in (1, 2) or any(len(part) > 5 for part in parts):
                    return False
                lower, upper = int(parts[0]), int(parts[-1])
                if lower < 0 or upper < lower or upper > 65535:
                    return False
                supported_count += upper - lower + 1
                if supported_count > 256:
                    return False
    except (AttributeError, IndexError, TypeError, ValueError):
        return False
    return True


class _PeerQueue(asyncio.Queue):
    """The upstream L2CAP-close sentinel must work even after a full queue."""

    def put_nowait(self, item):
        if item is None and self.full():
            self.get_nowait()
        return super().put_nowait(item)


class HandsFreeProtocol(hfp.HfProtocol):
    pending_codec = None

    def __init__(self, dlc, configuration):
        self.stopped = False
        self._bluehead_command_responses = 0
        self._bluehead_command_gate = asyncio.Lock()
        super().__init__(dlc, configuration)
        self.response_queue = asyncio.Queue(maxsize=MAX_AT_QUEUE)
        self.unsolicited_queue = _PeerQueue(maxsize=MAX_AT_QUEUE)

    def stop(self):
        """Stop this SLC synchronously; the owner closes its DLC/multiplexer."""
        if self.stopped:
            return False
        self.stopped = True
        self.read_buffer.clear()
        for queue in (self.response_queue, self.unsolicited_queue):
            while not queue.empty():
                queue.get_nowait()
        # Wake both a pending AT command and the unsolicited-response consumer.
        self.response_queue.put_nowait(hfp.AtResponse("ERROR", []))
        self.unsolicited_queue.put_nowait(None)
        return True

    def abort(self, reason_code="invalid_response"):
        """Idempotent peer-local failure; emit only predefined, non-PII text."""
        if not self.stop():
            return False
        self.emit("protocol_error", _PROTOCOL_ERRORS.get(reason_code, _PROTOCOL_ERRORS["invalid_response"]))
        return True

    def _read_at(self, data):
        if self.stopped:
            return
        if not isinstance(data, (bytes, bytearray, memoryview)):
            self.abort()
            return
        size = data.nbytes if isinstance(data, memoryview) else len(data)
        if size > MAX_AT_BUFFER - len(self.read_buffer):
            self.abort("buffer_limit")
            return
        self.read_buffer.extend(data)
        while self.read_buffer:
            marker = self.read_buffer.find(b"\r\n")
            if marker < 0:
                # A trailing CR may be the first half of the line delimiter.
                if len(self.read_buffer) > MAX_AT_LINE + int(self.read_buffer[-1] == 13):
                    self.abort("line_limit")
                return
            if marker > MAX_AT_LINE:
                self.abort("line_limit")
                return
            # Consume before parsing: malformed bytes cannot remain at the head
            # of the buffer and make every subsequent RFCOMM callback fail.
            line = self.read_buffer[:marker]
            del self.read_buffer[: marker + 2]
            if not line:
                continue
            try:
                response = hfp.AtResponse.parse_from(line)
            except Exception:
                self.abort("invalid_response")
                return
            if not response.code:
                self.abort("invalid_response")
                return
            if response.code == "+CIND" and self.pending_command == "AT+CIND=?" and not _bounded_cind_test(response):
                self.abort("invalid_response")
                return
            if self.pending_command and (
                response.code in hfp.STATUS_CODES or response.code in self.pending_command
            ):
                self._bluehead_command_responses += 1
                if self._bluehead_command_responses > MAX_AT_QUEUE:
                    self.abort("command_limit")
                    return
                queue = self.response_queue
            elif response.code in _UNSOLICITED_CODES:
                queue = self.unsolicited_queue
            else:
                # Vendor result codes are legal to ignore. Do not forward their
                # text to Bumble's unknown-response logger or retain them.
                continue
            try:
                queue.put_nowait(response)
            except asyncio.QueueFull:
                self.abort("queue_limit")
                return

    async def execute_command(self, cmd, timeout=1.0, response_type=hfp.AtResponseType.NONE):
        # A waiter queued before stop() must not send after the current command
        # has been woken. The base command_lock alone cannot recheck this.
        async with self._bluehead_command_gate:
            if self.stopped:
                raise hfp.HfpProtocolError("HFP service is closed")
            self._bluehead_command_responses = 0
            try:
                # The upstream per-response timeout resets after every partial
                # response. Bound the complete command and its result list.
                async with asyncio.timeout(timeout):
                    return await super().execute_command(cmd, timeout=timeout, response_type=response_type)
            except TimeoutError:
                self.abort("command_timeout")
                raise
            except asyncio.CancelledError:
                # A late OK cannot safely acknowledge another command after a
                # cancelled transaction. Reopen this SLC rather than reusing it.
                self.abort("command_cancelled")
                raise

    async def initiate_slc(self):
        try:
            return await super().initiate_slc()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.abort("invalid_response")
            raise hfp.HfpProtocolError("HFP service negotiation failed") from None

    async def run(self):
        # Keep upstream parsing/dispatch, but avoid logging a malformed response
        # or a caller number embedded in an exception's original message.
        try:
            if not self._slc_initialized:
                await self.initiate_slc()
            while not self.stopped:
                await self.handle_unsolicited()
        except hfp.HfProtocol.HfLoopTermination:
            self.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.abort("invalid_response")

    @property
    def audio_codec(self):
        return self.pending_codec if self.pending_codec is not None else self.active_codec

    async def setup_codec_connection(self, codec_id):
        # The AG may send its eSCO request immediately after receiving AT+BCS,
        # before the coroutine handling its OK gets scheduled. Bind that request
        # to the selected codec, rather than interpreting transparency as mSBC.
        if codec_id not in self.supported_audio_codecs:
            return await super().setup_codec_connection(codec_id)
        self.pending_codec = hfp.AudioCodec(codec_id)
        try:
            prepare = getattr(self, "before_codec", None)
            if prepare is not None and not await prepare(self.pending_codec):
                raise hfp.HfpProtocolError("Voice admission was not ready")
            if self.stopped:
                raise hfp.HfpProtocolError("HFP service is closed")
            await super().setup_codec_connection(codec_id)
        except BaseException:
            self.emit("codec_failed", self.pending_codec)
            raise
        finally:
            self.pending_codec = None


def selected_codec(protocol):
    return getattr(protocol, "audio_codec", protocol.active_codec)


@dataclass(frozen=True)
class FallbackResult:
    """An AT request outcome, never an assertion that eSCO was established."""

    status: str
    failed_codec: hfp.AudioCodec
    offered_codecs: tuple[hfp.AudioCodec, ...]
    reconnect_required: bool = False

    @property
    def requested(self) -> bool:
        return self.status == "requested"


async def renegotiate_fallback(
    protocol: hfp.HfProtocol,
    *,
    failed_codec: hfp.AudioCodec | int | None = None,
    timeout: float = 8.0,
) -> FallbackResult:
    """Temporarily withdraw the failed codec and request real AG negotiation.

    Call only for a codec/transport incompatibility after the failed link has
    gone away. Resources busy, user cancellation and ordinary disconnects must
    not withdraw capabilities. HFP 1.9 section 4.11 requires BAC/BCC/BCS: changing
    ``active_codec`` locally would interpret the next transparent link wrongly.

    Each failed codec is attempted once per SLC. An ambiguous AT timeout requires
    a new SLC because a late OK cannot safely be attributed to another command.
    Cancellation propagates so the owner can close the protocol normally.
    """
    selected = hfp.AudioCodec(selected_codec(protocol))
    failed = selected if failed_codec is None else hfp.AudioCodec(failed_codec)
    original = tuple(protocol.supported_audio_codecs)
    if failed != selected:
        return FallbackResult("stale", failed, original)
    if not (
        protocol.supports_hf_feature(hfp.HfFeature.CODEC_NEGOTIATION)
        and protocol.supports_ag_feature(hfp.AgFeature.CODEC_NEGOTIATION)
    ):
        return FallbackResult("exhausted", failed, original)
    lock = getattr(protocol, "_bluehead_fallback_lock", None)
    if lock is None:
        lock = protocol._bluehead_fallback_lock = asyncio.Lock()
        protocol._bluehead_fallback_attempted = set()
    if lock.locked():
        return FallbackResult("busy", failed, original)
    async with lock:
        attempted = protocol._bluehead_fallback_attempted
        if failed in attempted or failed not in original or failed == hfp.AudioCodec.CVSD:
            return FallbackResult("exhausted", failed, original)
        offered = tuple(codec for codec in original if int(codec) < int(failed))
        if hfp.AudioCodec.CVSD not in offered:
            offered = (hfp.AudioCodec.CVSD, *offered)
        attempted.add(failed)
        # Publish before BAC: an AG can immediately send BCS and then eSCO,
        # without waiting for this coroutine to resume after BAC's OK.
        protocol.supported_audio_codecs = list(offered)
        bac_accepted = False
        try:
            async with asyncio.timeout(timeout):
                await protocol.execute_command(_bac(offered), timeout=timeout)
                bac_accepted = True
                await protocol.execute_command("AT+BCC", timeout=timeout)
        except asyncio.CancelledError:
            protocol.supported_audio_codecs = list(original)
            raise
        except TimeoutError:
            protocol.supported_audio_codecs = list(original)
            return FallbackResult("timeout", failed, original, reconnect_required=True)
        except Exception:
            protocol.supported_audio_codecs = list(original)
            restore_failed = False
            if bac_accepted:
                try:
                    async with asyncio.timeout(min(timeout, 1.0)):
                        await protocol.execute_command(_bac(original), timeout=min(timeout, 1.0))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    restore_failed = True
            return FallbackResult("rejected", failed, original, reconnect_required=restore_failed)
        return FallbackResult("requested", failed, offered)


def _bac(codecs):
    return "AT+BAC=" + ",".join(str(int(codec)) for codec in codecs)


def handsfree_records(handle, channel, configuration):
    records = hfp.make_hf_sdp_records(handle, channel, configuration, version=hfp.ProfileVersion.V1_9)
    if hfp.AudioCodec.LC3_SWB in configuration.supported_audio_codecs:
        for attribute in records:
            if attribute.id == sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID:
                attribute.value = sdp.DataElement.unsigned_integer_16(
                    int(attribute.value.value) | int(hfp.HfSdpFeature.SUPER_WIDE_BAND))
    return records
