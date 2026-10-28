namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void CodecBitrateChecks(Dictionary<string, bool> checks)
    {
        var settings = new Dictionary<string, CodecParameters>();
        checks["codec_choice_labels_explain_quality_and_bandwidth_without_numbers"] = CodecPolicyWindow.Codecs.All(item => CodecBitrateLimits.Label(item.Code, settings).StartsWith(item.Name + " · ") && CodecBitrateLimits.Label(item.Code, settings).Contains("带宽") && !CodecBitrateLimits.Label(item.Code, settings).Contains("kb/s") && !CodecBitrateLimits.Label(item.Code, settings).Contains('≤'));
        checks["codec_choice_guidance_keeps_automatic_selection_unchanged"] = CodecBitrateLimits.Label("auto", settings) == "自动 · 优先高清";
        checks["codec_choices_expose_different_quality_and_bandwidth_tendencies"] = new[] { "ldac", "lhdc5", "aptx_hd" }.All(code => CodecBitrateLimits.Label(code, settings).Contains("高音质")) && CodecBitrateLimits.Label("aptx", settings).Contains("均衡/中等带宽") && CodecBitrateLimits.Label("aac", settings).Contains("均衡/较低带宽") && CodecBitrateLimits.Label("sbc", settings).Contains("基础/中等带宽");
        settings["sbc"] = new() { quality = "bandwidth" };
        settings["aac"] = new() { bitrate = 128000 };
        settings["ldac"] = new() { rate = 96000 };
        var before = System.Text.Json.JsonSerializer.Serialize(settings);
        checks["lower_bandwidth_sbc_setting_has_matching_selection_guidance"] = CodecBitrateLimits.Label("sbc", settings).Contains("较低带宽") && CodecBitrateLimits.Label("aac_192", settings) == CodecBitrateLimits.Label("aac", settings);
        foreach (var (code, _) in CodecPolicyWindow.Codecs) { CodecBitrateLimits.Label(code, settings); CodecBitrateLimits.Description(code, settings); }
        checks["codec_choice_guidance_never_modifies_audio_parameters"] = before == System.Text.Json.JsonSerializer.Serialize(settings);
        checks["codec_choice_help_explains_limits_and_points_to_actual_audio_data"] = CodecPolicyWindow.Codecs.All(item => CodecBitrateLimits.Description(item.Code, settings).Contains("选择建议") && CodecBitrateLimits.Description(item.Code, settings).Contains("发送设备") && CodecBitrateLimits.Description(item.Code, settings).Contains("实际码率请看设备卡片") && !CodecBitrateLimits.Description(item.Code, settings).Contains("理论码率"));
    }
}
