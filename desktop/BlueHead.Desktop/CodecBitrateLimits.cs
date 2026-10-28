using System.Globalization;

namespace BlueHead.Desktop;

// These are control-plane estimates, never measurements of a running stream.
// LDAC rate families: Sony's AOSP libldac/inc/ldacBT.h.
// https://android.googlesource.com/platform/external/libldac/+/master/inc/ldacBT.h
// HQ uses 330-byte frames: 44.1/88.2 kHz is exactly 909.5625 kb/s
// (the public API/documentation calls that tier "909").
// https://android.googlesource.com/platform/external/libldac/+/master/src/ldacBT_internal.c
// aptX/HD: fixed 8/12 encoded bits per stereo sample pair (48 kHz: 384/576 kb/s).
// https://www.aptx.com/about-aptx  https://www.aptx.com/aptx-hd
// SBC: A2DP's two-channel upper bound; do not mistake the common joint-stereo
// profile for a limit on every allowed channel mode/block/subband combination.
// https://datatracker.ietf.org/doc/draft-ietf-payload-rtp-sbc/05/
// LHDC V5's source encoder allows 64–1000 kb/s; rate/depth alone do not request
// its private encoder quality tier. The receiver advertises no lower tier cap.
// https://www.lhdc.co/en/news
internal static class CodecBitrateLimits
{
    private static CodecParameters Parameters(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
        => values is not null && values.TryGetValue(CodecPolicyWindow.Canonical(codec), out var value) ? value : new();

    internal static decimal MaximumKbps(string codec, CodecParameters? parameters = null)
    {
        var value = parameters ?? new();
        var canonical = CodecPolicyWindow.Canonical(codec);
        var rate = value.rate == 44100 ? 44100 : 48000;
        return canonical switch
        {
            "sbc" => 512m,
            "aac" => Math.Min(value.bitrate > 0 ? Math.Min(value.bitrate, 320000) : codec == "aac_192" && value.quality == "auto" ? 192000 : 320000,
                value.quality switch { "balanced" => 256000, "bandwidth" => 192000, _ => 320000 }) / 1000m,
            "aptx" => rate * .008m,
            "aptx_hd" => rate * .012m,
            "ldac" => value.rate is 44100 or 88200 ? 909.5625m : 990m,
            "lhdc5" => 1000m,
            _ => 0m
        };
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

    private static string Number(decimal value) => value.ToString("0.###", CultureInfo.InvariantCulture);

    internal static string Label(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "自动 · 优先高清";
        var canonical = CodecPolicyWindow.Canonical(codec);
        var name = CodecPolicyWindow.Codecs.FirstOrDefault(item => item.Code == canonical).Name;
        if (name is null) return codec;
        // Round a displayed upper bound upward, never below the frame-derived
        // value. A single decimal keeps every item within the compact picker.
        var maximum = Math.Ceiling(MaximumKbps(codec, Parameters(codec, values)) * 10m) / 10m;
        return name + " · ≤" + Number(maximum) + " kb/s" + (canonical == "sbc" ? "*" : "");
    }

    internal static string Description(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "优先协商双方可用的高清媒体编码。各编码旁显示当前保存参数下的理论码率上限；当前实际码率请看设备卡片。";
        var value = Parameters(codec, values);
        var canonical = CodecPolicyWindow.Canonical(codec);
        var rate = value.rate > 0 ? Number(value.rate / 1000m) + " kHz" : "自动采样率（按 48 kHz 参考）";
        var explanation = canonical switch
        {
            "sbc" => "*512 kb/s 是 A2DP 双声道协议上限；实际还取决于声道模式、块数和子带数。按常用的联合立体声、16 块、8 子带及当前质量设置计算，" + rate + "的上限参考为 " + Number(CommonSbcKbps(value)) + " kb/s。",
            "aac" => "采用已设置的码率上限与质量上限中较低的一项，最高 320 kb/s。发送设备可以使用更低码率。",
            "aptx" or "aptx_hd" => "固定编码比例；采样率会改变码率。44.1 kHz 时为 " + (canonical == "aptx" ? "352.8" : "529.2") + " kb/s，48 kHz 时为 " + (canonical == "aptx" ? "384" : "576") + " kb/s。",
            "ldac" => "44.1 / 88.2 kHz 的最高档通常称为 909 kb/s，按帧长计算为 909.5625 kb/s，选项中向上取整显示为 909.6；48 / 96 kHz 为 990 kb/s。实际质量档位和码率由发送设备选择。",
            "lhdc5" => "LHDC 5.0 的理论上限为 1000 kb/s。选择采样率或样本位数不会固定其发送码率；质量档位和实际码率由发送设备选择。",
            _ => "实际格式由双方协商。"
        };
        return "当前保存参数下的理论上限：" + Number(MaximumKbps(codec, value)) + " kb/s（不是正在播放的实际码率）。\n" + explanation + "\n可点击媒体编码右侧的设置按钮调节；修改后断开并重新连接设备生效。不同编码的码率不能直接用来比较音质。";
    }
}
