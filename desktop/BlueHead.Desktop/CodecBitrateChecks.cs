namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void CodecBitrateChecks(Dictionary<string, bool> checks)
    {
        checks["codec_choices_show_only_codec_names"] = CodecPolicyWindow.Codecs.All(item => CodecBitrateLimits.Label(item.Code) == item.Name);
        checks["codec_choices_keep_automatic_and_legacy_aac_selection_names"] = CodecBitrateLimits.Label("auto") == "自动 · 优先高清" && CodecBitrateLimits.Label("aac_192") == "AAC";
    }
}
