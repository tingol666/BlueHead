namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void CodecBitrateChecks(Dictionary<string, bool> checks)
    {
        var defaults = new Dictionary<string, CodecParameters>();
        checks["codec_bitrate_labels_cover_all_classic_codecs_without_changing_auto"] = CodecPolicyWindow.Codecs.All(item => CodecBitrateLimits.Label(item.Code, defaults).StartsWith(item.Name + " · ≤") && CodecBitrateLimits.Label(item.Code, defaults).Contains("kb/s")) && CodecBitrateLimits.Label("auto", defaults) == "自动 · 优先高清";
        checks["aptx_bitrate_tracks_fixed_compression_and_sample_rate"] = CodecBitrateLimits.MaximumKbps("aptx", new() { rate = 44100 }) == 352.8m && CodecBitrateLimits.MaximumKbps("aptx", new() { rate = 48000 }) == 384m && CodecBitrateLimits.MaximumKbps("aptx_hd", new() { rate = 44100 }) == 529.2m && CodecBitrateLimits.MaximumKbps("aptx_hd") == 576m;
        checks["ldac_bitrate_uses_correct_44100_and_48000_families"] = new[] { 44100, 88200 }.All(rate => CodecBitrateLimits.MaximumKbps("ldac", new() { rate = rate }) == 330m * 8 * rate / (rate == 44100 ? 128 : 256) / 1000) && new[] { 0, 48000, 96000 }.All(rate => CodecBitrateLimits.MaximumKbps("ldac", new() { rate = rate }) == 990m);
        checks["aac_bitrate_display_uses_lower_explicit_and_quality_ceiling"] = CodecBitrateLimits.MaximumKbps("aac", new() { bitrate = 320000, quality = "balanced" }) == 256m && CodecBitrateLimits.MaximumKbps("aac", new() { bitrate = 128000, quality = "balanced" }) == 128m && CodecBitrateLimits.MaximumKbps("aac", new() { quality = "bandwidth" }) == 192m && CodecBitrateLimits.MaximumKbps("aac") == 320m;
        checks["aac_legacy_alias_is_not_lost_or_forced_over_explicit_settings"] = CodecBitrateLimits.MaximumKbps("aac_192") == 192m && CodecBitrateLimits.MaximumKbps("aac_192", new() { bitrate = 320000 }) == 320m && CodecBitrateLimits.MaximumKbps("aac_192", new() { quality = "high" }) == 320m;
        checks["sbc_bitrate_distinguishes_protocol_bound_from_common_profile"] = CodecBitrateLimits.MaximumKbps("sbc", new() { quality = "bandwidth" }) == 512m && CodecBitrateLimits.CommonSbcKbps() == 357m && CodecBitrateLimits.CommonSbcKbps(new() { quality = "balanced" }) == 279m && CodecBitrateLimits.CommonSbcKbps(new() { rate = 44100, quality = "bandwidth" }) == 212.23125m && CodecBitrateLimits.Label("sbc", defaults).EndsWith("kb/s*") && CodecBitrateLimits.Description("sbc", defaults).Contains("按 48 kHz 参考");
        checks["lhdc_rate_and_depth_do_not_fabricate_source_quality_tier"] = new[] { 44100, 48000, 96000, 192000 }.All(rate => new[] { 16, 24 }.All(depth => CodecBitrateLimits.MaximumKbps("lhdc5", new() { rate = rate, depth = depth }) == 1000m)) && CodecBitrateLimits.Description("lhdc5", defaults).Contains("由发送设备选择");
        defaults["aac"] = new() { bitrate = 128000 };
        defaults["ldac"] = new() { rate = 88200 };
        checks["codec_bitrate_labels_read_live_saved_parameters_independently"] = CodecBitrateLimits.Label("aac", defaults) == "AAC · ≤128 kb/s" && CodecBitrateLimits.Label("ldac", defaults) == "LDAC · ≤909.6 kb/s" && CodecBitrateLimits.Label("aptx_hd", defaults) == "aptX HD · ≤576 kb/s";
        checks["ldac_compact_upper_bound_never_rounds_below_encoded_frame_rate"] = decimal.Parse(CodecBitrateLimits.Label("ldac", defaults).Split('≤')[1].Split(' ')[0], System.Globalization.CultureInfo.InvariantCulture) >= CodecBitrateLimits.MaximumKbps("ldac", defaults["ldac"]);
        checks["codec_bitrate_help_distinguishes_theory_actual_rate_and_quality"] = CodecPolicyWindow.Codecs.All(item => CodecBitrateLimits.Description(item.Code, defaults).Contains("不是正在播放的实际码率") && CodecBitrateLimits.Description(item.Code, defaults).Contains("不同编码的码率不能直接用来比较音质"));
    }
}
