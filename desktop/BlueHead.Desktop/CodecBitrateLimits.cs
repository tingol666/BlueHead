using System.Globalization;

namespace BlueHead.Desktop;

// These are control-plane bitrate limits, never measurements of a running
// stream or a promise that the source will select its highest quality tier.
// LDAC rate families: Sony's AOSP libldac/inc/ldacBT.h.
// https://android.googlesource.com/platform/external/libldac/+/master/inc/ldacBT.h
// aptX/HD: fixed 8/12 encoded bits per stereo sample pair (48 kHz: 384/576 kb/s).
// https://www.aptx.com/about-aptx  https://www.aptx.com/aptx-hd
// Sony's HQ frame table gives 330 bytes per 128/256 samples; 44.1/88.2 kHz
// therefore means 909.5625 kb/s, displayed upward to 909.6 rather than 909.
// https://android.googlesource.com/platform/external/libldac/+/master/src/ldacBT_internal.c
// LHDC V5's 1000 kb/s tier remains source-controlled. Rate/depth do not select it.
// https://www.lhdc.co/en/news
internal static class CodecBitrateLimits
{
    private static CodecParameters Parameters(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
        => values is not null && values.TryGetValue(CodecPolicyWindow.Canonical(codec), out var value) ? value : new();

    internal static decimal MaximumKbps(string codec, CodecParameters? parameters = null)
    {
        var value = parameters ?? new();
        var rate = value.rate == 44100 ? 44100 : 48000;
        return CodecPolicyWindow.Canonical(codec) switch
        {
            "aptx" => rate * .008m,
            "aptx_hd" => rate * .012m,
            "ldac" => value.rate is 44100 or 88200 ? 909.5625m : 990m,
            "lhdc5" => 1000m,
            _ => 0m
        };
    }

    private static string Number(decimal value) => value.ToString("0.####", CultureInfo.InvariantCulture);

    internal static string Label(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "自动 · 优先高清";
        var canonical = CodecPolicyWindow.Canonical(codec);
        var name = CodecPolicyWindow.Codecs.FirstOrDefault(item => item.Code == canonical).Name;
        if (name is null) return codec;
        if (canonical is "aac" or "sbc") return name;
        var maximum = Math.Ceiling(MaximumKbps(codec, Parameters(codec, values)) * 10m) / 10m;
        return name + " · ≤" + Number(maximum) + " kb/s";
    }

    internal static string Description(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "优先协商双方可用的高清媒体编码。部分选项旁显示当前保存参数下的理论码率上限，实际接收码率请看设备卡片。";
        var value = Parameters(codec, values);
        var canonical = CodecPolicyWindow.Canonical(codec);
        var explanation = canonical switch
        {
            "sbc" => "兼容性较好的经典媒体编码。实际码率由发送设备协商，还取决于声道方式和质量设置。",
            "aac" => "可在 AAC 旁的小齿轮中设置码率和质量上限；实际发送码率仍由发送设备决定。",
            "aptx" or "aptx_hd" => "固定编码比例，采样率会改变码率。采样率为自动时显示允许范围内的理论最高值；实际采样率和码率请看设备卡片。",
            "ldac" => "44.1 / 88.2 kHz 的理论最高档约 909.6 kb/s，48 / 96 kHz 为 990 kb/s；实际质量档位和码率由发送设备选择。",
            "lhdc5" => "理论最高档为 1000 kb/s。采样率或样本位数不会固定其发送码率；实际质量档位和码率由发送设备选择。",
            _ => "实际格式由双方协商。"
        };
        var reference = canonical is "aac" or "sbc" ? "" : "当前保存参数下的理论码率上限：" + Number(MaximumKbps(codec, value)) + " kb/s，不是当前连接的实测值。\n";
        return reference + explanation + "\n实际码率请看设备卡片。点击媒体编码右侧的设置按钮可调节参数；修改后断开并重新连接设备生效。不同编码的码率不能直接用来比较音质。";
    }
}
