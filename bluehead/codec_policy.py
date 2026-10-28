"""Persistent capability/preference model for the next Classic connection.

The sending device still owns its encoder. Only parameters present in the wire
capability are writable; quality names never pretend to control a private source
encoder. These are control-plane values, not PCM or realtime callbacks.
"""

from dataclasses import dataclass, asdict
from bumble import a2dp, avdtp
from .protocol import capabilities, negotiated, PacketError

CODECS = ('sbc', 'aac', 'aptx', 'aptx_hd', 'ldac', 'lhdc5')
ORDER = ('lhdc5', 'ldac', 'aptx_hd', 'aptx', 'aac', 'sbc')
NAMES = dict(sbc='SBC', aac='AAC', aptx='aptX', aptx_hd='aptX HD', ldac='LDAC', lhdc5='LHDC 5.0')
RATES = {name: (44100, 48000) for name in CODECS}
RATES['ldac'] = (44100, 48000, 88200, 96000)
RATES['lhdc5'] = (44100, 48000, 96000, 192000)


def canonical(codec):
    return 'aac' if codec == 'aac_192' else codec


def permitted(settings):
    value = getattr(settings, 'allowed_codecs', None)
    if value is None:
        value = ['sbc', 'aac', 'aptx', 'aptx_hd']
        if settings.ldac_enabled:
            value.append('ldac')
        if settings.lhdc5_enabled:
            value.append('lhdc5')
    if (not isinstance(value, list) or len(value) > len(CODECS)
            or any(not isinstance(item, str) or item not in CODECS for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('允许编码列表无效')
    return set(value) | {'sbc'}


def ordered(settings, availability=None):
    allowed = permitted(settings)
    if availability is not None:
        allowed &= {name for name, state in availability.items() if state.get('available') is True}
    preferred = canonical(settings.codec)
    sequence = ([preferred] if preferred in allowed else []) + list(ORDER)
    return list(dict.fromkeys(name for name in sequence if name in allowed))


@dataclass(frozen=True)
class Selection:
    codec: str = 'auto'
    rate: int = 0
    depth: int = 0
    bitrate: int = 0
    quality: str = 'auto'

    @classmethod
    def parse(cls, values):
        if not isinstance(values, dict) or set(values) - {'codec', 'rate', 'depth', 'bitrate', 'quality'}:
            raise ValueError('编码参数格式无效')
        selection = cls(**values)
        if selection.codec not in ('auto', *CODECS):
            raise ValueError('未知媒体编码')
        if any(type(item) is not int for item in (selection.rate, selection.depth, selection.bitrate)):
            raise ValueError('音频参数必须为整数')
        if selection.rate and selection.rate not in RATES.get(selection.codec, ()):
            raise ValueError('这个编码不支持所选采样率')
        if selection.depth not in (0, 16, 24) or selection.depth and selection.codec != 'lhdc5':
            raise ValueError('这个编码不提供独立位深选择')
        if selection.bitrate and (selection.codec != 'aac' or not 64000 <= selection.bitrate <= 320000):
            raise ValueError('这个编码不提供所选码率上限')
        if selection.quality not in ('auto', 'high', 'balanced', 'bandwidth'):
            raise ValueError('未知质量选项')
        if selection.quality != 'auto' and selection.codec not in ('sbc', 'aac'):
            raise ValueError('该编码的质量档位由发送设备控制；可调整双方支持的采样率')
        if selection.codec == 'auto' and (selection.rate or selection.depth or selection.bitrate):
            raise ValueError('请先选择编码再调整参数')
        return selection

    def wire(self):
        return asdict(self)


def parameters(values):
    """Validate and detach persistent settings; codec keys supply the codec.

    Source-private encoder modes are deliberately rejected, not silently saved
    as though they could alter LDAC/LHDC transmission quality.
    """
    if not isinstance(values, dict) or len(values) > len(CODECS):
        raise ValueError('各编码的音质设置无效')
    result = {}
    for name, detail in values.items():
        if not isinstance(name, str) or name not in CODECS or not isinstance(detail, dict):
            raise ValueError('各编码的音质设置无效')
        if set(detail) - {'rate', 'depth', 'bitrate', 'quality'}:
            raise ValueError('各编码的音质设置包含不支持的选项')
        selection = Selection.parse(dict(codec=name, **detail))
        result[name] = {key: value for key, value in selection.wire().items() if key != 'codec'}
    return result


def selections(settings):
    """Snapshot settings once per signalling connection, never per media packet."""
    return {name: Selection(codec=name, **detail)
            for name, detail in parameters(getattr(settings, 'codec_parameters', {})).items()}


def replace_info(cap, raw):
    kind = cap.media_codec_type
    if kind == a2dp.CodecType.NON_A2DP:
        info = a2dp.VendorSpecificMediaCodecInformation(
            int.from_bytes(raw[:4], 'little'), int.from_bytes(raw[4:6], 'little'), raw[6:])
    elif kind == a2dp.CodecType.SBC:
        info = a2dp.SbcMediaCodecInformation.from_bytes(raw)
    elif kind == a2dp.CodecType.MPEG_2_4_AAC:
        info = a2dp.AacMediaCodecInformation.from_bytes(raw)
    else:
        raise PacketError('Unknown Classic codec')
    return avdtp.MediaCodecCapabilities(avdtp.MediaType.AUDIO, kind, info)


def offered(codec, selection=None, *, legacy_aac_limit=False):
    codec = canonical(codec)
    # The historic AAC-192 preset remains intact, but a deliberate per-codec
    # bitrate or quality override replaces that legacy preset on reconnect.
    legacy = (codec == 'aac' and legacy_aac_limit
              and (selection is None or not selection.bitrate and selection.quality == 'auto'))
    cap = capabilities('aac_192' if legacy else codec)
    if selection is None or selection.codec not in (codec, 'auto'):
        return cap
    raw = bytearray(bytes(cap.media_codec_information))
    if selection.rate:
        mask = {44100: 0x20, 48000: 0x10, 88200: 8, 96000: 4, 192000: 1}[selection.rate]
        if codec in ('sbc', 'aptx', 'aptx_hd'):
            raw[0 if codec == 'sbc' else 6] = mask | (raw[0 if codec == 'sbc' else 6] & 15)
        elif codec == 'aac':
            frequency = 16 if selection.rate == 44100 else 8
            raw[1], raw[2] = frequency >> 4, ((frequency & 15) << 4) | (raw[2] & 15)
        else:
            raw[6] = mask
    if codec == 'sbc' and selection.quality != 'auto':
        raw[3] = dict(high=53, balanced=40, bandwidth=32)[selection.quality]
    if codec == 'aac':
        limit = min(selection.bitrate or 320000, dict(auto=320000, high=320000, balanced=256000,
                                                    bandwidth=192000)[selection.quality])
        original = int.from_bytes(raw[3:], 'big') & 0x7fffff
        raw[3:] = ((min(limit, original) | (raw[3] & 0x80) << 16)).to_bytes(3, 'big')
    if codec == 'lhdc5' and selection.depth:
        raw[7] = (raw[7] & ~7) | {16: 4, 24: 2}[selection.depth]
    return replace_info(cap, raw)


def validates(codec, config, limit):
    """The source cannot select formats/bounds we did not actually advertise."""
    fmt = negotiated(codec, config)
    chosen = next(cap for cap in config if isinstance(cap, avdtp.MediaCodecCapabilities))
    raw, allowed = bytes(chosen.media_codec_information), bytes(limit.media_codec_information)
    if codec == 'sbc':
        valid = (raw[0] & ~allowed[0] == 0 and raw[1] & ~allowed[1] == 0
                 and allowed[2] <= raw[2] <= raw[3] <= allowed[3])
    elif canonical(codec) == 'aac':
        value, ceiling = int.from_bytes(raw[3:], 'big') & 0x7fffff, int.from_bytes(allowed[3:], 'big') & 0x7fffff
        valid = (all(raw[i] & ~allowed[i] == 0 for i in (0, 1, 2))
                 and (not raw[3] & 0x80 or bool(allowed[3] & 0x80))
                 and (not value or value <= ceiling))
    else:
        valid = all(raw[i] & ~allowed[i] == 0 for i in range(6, len(raw)))
    if not valid:
        raise PacketError('对端选择的格式超出当前允许范围')
    return fmt


def codec_of(cap):
    for name in CODECS:
        baseline = capabilities(name)
        if cap.media_codec_type != baseline.media_codec_type:
            continue
        raw = bytes(cap.media_codec_information)
        if name in ('sbc', 'aac') or raw[:6] == bytes(baseline.media_codec_information)[:6]:
            return name
    return None


def select(codec, local, remote, selection=None):
    """Select a single legal configuration from both capability sets."""
    selection = selection or Selection(codec)
    raw, peer = bytearray(bytes(local.media_codec_information)), bytes(remote.media_codec_information)
    if codec_of(remote) != codec or len(raw) != len(peer):
        raise PacketError('发送设备没有提供这个编码')

    def one(mask, preferences):
        for flag in preferences:
            if flag & mask:
                return flag
        raise PacketError('双方没有共同支持的音频格式')

    if codec == 'sbc':
        raw[0] = (one((raw[0] & peer[0]) >> 4, (1, 2)) << 4) | one(raw[0] & peer[0] & 15, (1, 2, 4, 8))
        common = raw[1] & peer[1]
        raw[1] = (one(common >> 4, (1, 2, 4, 8)) << 4) | (one((common >> 2) & 3, (1, 2)) << 2) | one(common & 3, (1, 2))
        raw[2], raw[3] = max(raw[2], peer[2]), min(raw[3], peer[3])
        if raw[2] > raw[3]:
            raise PacketError('双方没有共同支持的 SBC 位池范围')
    elif codec == 'aac':
        raw[0] = one(raw[0] & peer[0], (0x40, 0x80))
        frequency = one(((raw[1] << 4) | (raw[2] >> 4)) & ((peer[1] << 4) | (peer[2] >> 4)), (8, 16))
        channels = one(raw[2] & peer[2] & 12, (4,))
        raw[1], raw[2] = frequency >> 4, ((frequency & 15) << 4) | channels
        values = [int.from_bytes(value[3:], 'big') & 0x7fffff for value in (raw, peer)]
        ceiling = min(value for value in values if value) if any(values) else 320000
        raw[3:] = (ceiling | ((raw[3] & peer[3] & 0x80) << 16)).to_bytes(3, 'big')
    elif codec in ('aptx', 'aptx_hd'):
        raw[6] = (one((raw[6] & peer[6]) >> 4, (1, 2)) << 4) | one(raw[6] & peer[6] & 15, (2,))
    else:
        raw[6] = one(raw[6] & peer[6], (1, 4, 8, 0x10, 0x20) if codec == 'lhdc5' else (4, 8, 0x10, 0x20))
        if codec == 'ldac':
            raw[7] = one(raw[7] & peer[7], (1,))
        else:
            raw[7] = (raw[7] & ~7) | one(raw[7] & peer[7] & 7, (2, 4))
            if peer[8] & 0x11 != 0x11:
                raise PacketError('双方没有共同支持的 LHDC 5.0 帧格式')
    cap = replace_info(local, raw)
    validates(codec, [cap], local)
    validates(codec, [cap], remote)
    return cap


def metadata(name, *, available=True, reason=''):
    return dict(codec=name, name=NAMES[name], available=available, reason=reason,
                rates=RATES[name], depths=(16, 24) if name == 'lhdc5' else (),
                quality_writable=name in ('sbc', 'aac'), bitrate_writable=name == 'aac',
                quality_reason='' if name in ('sbc', 'aac') else '质量档位与实际发送码率由发送设备决定。')
