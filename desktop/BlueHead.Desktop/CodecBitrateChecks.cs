namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void CodecBitrateChecks(Dictionary<string, bool> checks)
    {
        var defaults = new Dictionary<string, CodecParameters>();
        checks["codec_labels_keep_two_plain_four_theoretical_and_auto_unchanged"] = CodecPolicyWindow.Codecs.All(item => item.Code is "aac" or "sbc" ? CodecBitrateLimits.Label(item.Code, defaults) == item.Name : CodecBitrateLimits.Label(item.Code, defaults).StartsWith(item.Name + " · ≤") && CodecBitrateLimits.Label(item.Code, defaults).Contains("kb/s")) && CodecBitrateLimits.Label("auto", defaults) == "自动 · 优先高清";
        checks["aptx_theoretical_bitrate_tracks_fixed_ratio_and_sample_rate"] = CodecBitrateLimits.MaximumKbps("aptx", new() { rate = 44100 }) == 352.8m && CodecBitrateLimits.MaximumKbps("aptx", new() { rate = 48000 }) == 384m && CodecBitrateLimits.MaximumKbps("aptx_hd", new() { rate = 44100 }) == 529.2m && CodecBitrateLimits.MaximumKbps("aptx_hd") == 576m;
        checks["ldac_theoretical_bitrate_matches_frame_table_and_rate_family"] = new[] { 44100, 88200 }.All(rate => CodecBitrateLimits.MaximumKbps("ldac", new() { rate = rate }) == 330m * 8 * rate / (rate == 44100 ? 128 : 256) / 1000) && new[] { 0, 48000, 96000 }.All(rate => CodecBitrateLimits.MaximumKbps("ldac", new() { rate = rate }) == 990m);
        checks["lhdc_theoretical_limit_does_not_select_source_quality"] = new[] { 44100, 48000, 96000, 192000 }.All(rate => new[] { 16, 24 }.All(depth => CodecBitrateLimits.MaximumKbps("lhdc5", new() { rate = rate, depth = depth }) == 1000m)) && CodecBitrateLimits.Description("lhdc5", defaults).Contains("由发送设备选择");
        defaults["aac"] = new() { bitrate = 128000 };
        defaults["ldac"] = new() { rate = 88200 };
        defaults["sbc"] = new() { rate = 44100, quality = "bandwidth" };
        checks["codec_labels_follow_saved_parameters_without_numbers_on_aac_or_sbc"] = CodecBitrateLimits.Label("aac", defaults) == "AAC" && CodecBitrateLimits.Label("aac_192", defaults) == "AAC" && CodecBitrateLimits.Label("sbc", defaults) == "SBC" && CodecBitrateLimits.Label("ldac", defaults) == "LDAC · ≤909.6 kb/s" && CodecBitrateLimits.Label("aptx_hd", defaults) == "aptX HD · ≤576 kb/s";
        checks["ldac_displayed_upper_bound_never_rounds_below_encoded_frame_rate"] = decimal.Parse(CodecBitrateLimits.Label("ldac", defaults).Split('≤')[1].Split(' ')[0], System.Globalization.CultureInfo.InvariantCulture) >= CodecBitrateLimits.MaximumKbps("ldac", defaults["ldac"]);
        checks["codec_bitrate_help_distinguishes_theoretical_limits_from_live_measurement"] = CodecPolicyWindow.Codecs.All(item => (item.Code is "aac" or "sbc" || CodecBitrateLimits.Description(item.Code, defaults).Contains("理论码率上限") && CodecBitrateLimits.Description(item.Code, defaults).Contains("不是当前连接的实测值")) && CodecBitrateLimits.Description(item.Code, defaults).Contains("实际码率请看设备卡片") && CodecBitrateLimits.Description(item.Code, defaults).Contains("不同编码的码率不能直接用来比较音质"));
    }
}
