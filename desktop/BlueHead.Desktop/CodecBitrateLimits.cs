namespace BlueHead.Desktop;

// Selection guidance for common configurations, not stream measurements or
// an absolute quality ranking. This presentation helper never changes settings.
internal static class CodecBitrateLimits
{
    private static CodecParameters Parameters(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
        => values is not null && values.TryGetValue(CodecPolicyWindow.Canonical(codec), out var value) ? value : new();

    private static string Guidance(string codec, CodecParameters parameters)
    {
        return codec switch
        {
            "lhdc5" or "ldac" => "高音质/高带宽",
            "aptx_hd" => "高音质/较高带宽",
            "aptx" => "均衡/中等带宽",
            "aac" => "均衡/较低带宽",
            "sbc" => parameters.quality is "balanced" or "bandwidth" ? "基础/较低带宽" : "基础/中等带宽",
            _ => ""
        };
    }

    internal static string Label(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "自动 · 优先高清";
        var canonical = CodecPolicyWindow.Canonical(codec);
        var name = CodecPolicyWindow.Codecs.FirstOrDefault(item => item.Code == canonical).Name;
        if (name is null) return codec;
        return name + " · " + Guidance(canonical, Parameters(codec, values));
    }

    internal static string Description(string codec, IReadOnlyDictionary<string, CodecParameters>? values)
    {
        if (codec == "auto") return "优先协商双方可用的高清媒体编码。选项标签说明音质倾向与常用配置的蓝牙带宽占用；实际音频格式和码率请看设备卡片。";
        var canonical = CodecPolicyWindow.Canonical(codec);
        var explanation = canonical switch
        {
            "sbc" => "基础媒体编码，侧重广泛兼容。带宽还取决于发送设备选择的声道方式和质量参数；可在小齿轮中选择更省带宽的质量范围。",
            "aac" => "兼顾音质与较低的传输负担，适合多设备或信号不稳时尝试。小齿轮中可进一步限制码率，实际编码方式由发送设备决定。",
            "aptx" => "在音质和传输负担之间取均衡，常用配置下带宽占用中等。",
            "aptx_hd" => "侧重声音细节，常用配置下比均衡类编码占用更多蓝牙带宽。",
            "ldac" or "lhdc5" => "侧重保留声音细节，高音质档通常占用较多蓝牙带宽；多台设备同时播放时更容易受带宽限制。实际档位由发送设备选择。",
            _ => "实际格式由双方协商。"
        };
        return Guidance(canonical, Parameters(codec, values)) + "：" + explanation + "\n标签是常用配置下的选择建议，音源、发送设备和详细参数也会影响听感。实际码率请看设备卡片。点击媒体编码右侧的设置按钮可调节参数；修改后断开并重新连接设备生效。";
    }
}
