namespace BlueHead.Desktop;

// Shared names keep the home selector and connection settings consistent.
internal static class CodecBitrateLimits
{
    internal static string Label(string codec)
    {
        if (codec == "auto") return "自动 · 优先高清";
        var canonical = CodecPolicyWindow.Canonical(codec);
        return CodecPolicyWindow.Codecs.FirstOrDefault(item => item.Code == canonical).Name ?? codec;
    }
}
