using System.Globalization;

namespace BlueHead.Desktop;

// These are rounded common-use references, never stream measurements or a
// promise that the source will choose a particular bitrate. Keep the helper
// name stable because both codec pickers use it.
// LDAC rate families: Sony's AOSP libldac/inc/ldacBT.h.
// https://android.googlesource.com/platform/external/libldac/+/master/inc/ldacBT.h
// aptX/HD: fixed 8/12 encoded bits per stereo sample pair (48 kHz: 384/576 kb/s).
// https://www.aptx.com/about-aptx  https://www.aptx.com/aptx-hd
// SBC uses the common joint-stereo profile, not the protocol ceiling.
// https://datatracker.ietf.org/doc/draft-ietf-payload-rtp-sbc/05/
// AAC senders commonly use 256–320 kb/s; the reference is constrained by the
// receiver's saved limits. AOSP defaults to 320; it can also use VBR.
// https://android.googlesource.com/platform/packages/modules/Bluetooth/+/refs/heads/main/system/stack/a2dp/a2dp_aac.cc
// LHDC common quality tiers span roughly 400–1000 kb/s; ABR may go lower.
// https://www.lhdc.co/en/about
internal static class CodecBitrateLimits
{
    private static CodecParameters Parameters(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
        => values is not null && values.TryGetValue(CodecPolicyWindow.Canonical(codec), out var value) ? value : new();

    private static decimal AacLimitKbps(string codec, CodecParameters value)
    {
        return Math.Min(value.bitrate > 0 ? Math.Min(value.bitrate, 320000) : codec == "aac_192" && value.quality == "auto" ? 192000 : 320000,
            value.quality switch { "balanced" => 256000, "bandwidth" => 192000, _ => 320000 }) / 1000m;
    }

    internal static decimal CommonSbcKbps(CodecParameters? parameters = null)
    {
        var value = parameters ?? new();
        var rate = value.rate == 44100 ? 44100 : 48000;
        var bitpool = value.quality switch { "balanced" => 40, "bandwidth" => 32, _ => 53 };
        // Joint stereo, 16 blocks, 8 subbands, 2 channels. Includes SBC header
        // and scale factors, excludes RTP/L2CAP/USB transport overhead.
        var bytes = 4 + 8 + (8 + 16 * bitpool + 7) / 8;
        return bytes * 8m * rate / (16 * 8 * 1000);
    }

    internal static (int Low, int High) CommonRangeKbps(string codec, CodecParameters? parameters = null)
    {
        var value = parameters ?? new();
        var canonical = CodecPolicyWindow.Canonical(codec);
        decimal low, high;
        if (canonical == "sbc")
        {
            low = CommonSbcKbps(new() { rate = value.rate > 0 ? value.rate : 44100, quality = value.quality });
            high = CommonSbcKbps(value);
        }
        else if (canonical == "aac") { high = AacLimitKbps(codec, value); low = Math.Min(256m, high); }
        else if (canonical is "aptx" or "aptx_hd")
        {
            var ratio = canonical == "aptx" ? .008m : .012m;
            low = (value.rate > 0 ? value.rate : 44100) * ratio;
            high = (value.rate > 0 ? value.rate : 48000) * ratio;
        }
        else if (canonical == "ldac")
        {
            low = value.rate is 48000 or 96000 ? 330 : 303;
            high = value.rate is 44100 or 88200 ? 910 : 990;
        }
        else if (canonical == "lhdc5") { low = 400; high = 1000; }
        else { low = high = 0; }
        return ((int)Math.Round(low, MidpointRounding.AwayFromZero), (int)Math.Round(high, MidpointRounding.AwayFromZero));
    }

    private static string Range((int Low, int High) value) => value.Low == value.High
        ? value.Low.ToString(CultureInfo.InvariantCulture)
        : value.Low.ToString(CultureInfo.InvariantCulture) + "–" + value.High.ToString(CultureInfo.InvariantCulture);

    internal static string Label(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "自动 · 优先高清";
        var canonical = CodecPolicyWindow.Canonical(codec);
        var name = CodecPolicyWindow.Codecs.FirstOrDefault(item => item.Code == canonical).Name;
        if (name is null) return codec;
        return name + " · 约" + Range(CommonRangeKbps(codec, Parameters(codec, values))) + " kb/s";
    }

    internal static string Description(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "优先协商双方可用的高清媒体编码。选项中的数字是常用码率参考，不是当前连接的实测值。实际码率请看设备卡片。";
        var value = Parameters(codec, values);
        var canonical = CodecPolicyWindow.Canonical(codec);
        var explanation = canonical switch
        {
            "sbc" => "按常见的立体声播放配置估算，并结合已保存的采样率和质量设置。发送设备使用其他声道方式或调低码率时，实际值会不同。",
            "aac" => "不同发送设备的 AAC 编码方式有所不同；这里按常见播放码率提供参考，并遵守已保存的码率和质量限制。内容和设备不同，实际码率也会变化。",
            "aptx" or "aptx_hd" => "采样率选为自动时显示 44.1 与 48 kHz 的码率范围；选定采样率时显示对应的约数。",
            "ldac" => "通常有省带宽、均衡和高清几档，发送设备可以自动调节。这里按已保存的采样率显示这些常用档位的范围。",
            "lhdc5" => "这里显示常用音质档位的范围；发送设备自动调整带宽时，码率还可能更低。采样率或样本位数不会固定其码率，实际档位由发送设备选择。",
            _ => "实际格式由双方协商。"
        };
        return "常用码率参考：约 " + Range(CommonRangeKbps(codec, value)) + " kb/s，不是当前连接的实测值。\n" + explanation + "\n实际码率请看设备卡片。点击媒体编码右侧的设置按钮可调节参数；修改后断开并重新连接设备生效。不同编码的码率不能直接用来比较音质。";
    }
}
