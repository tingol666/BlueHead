using System.Diagnostics;
using System.Text.Json;

namespace BlueHead.Desktop;

// Advisory UI state only. No audio samples, signalling, codec changes or timers.
// A decoder can report one bad packet through several counters, so each window
// uses the largest counter delta instead of adding overlapping failure counts.
internal sealed class MediaHealthTracker
{
    internal const int MaximumStreams = 64;
    private static readonly string[] ClassicCounters = ["lost_packets", "dropped_packets", "decode_errors", "malformed_packets", "expired_packets", "sync_errors"];
    private static readonly string[] LeCounters = ["dropped", "errors", "concealed_blocks"];
    private sealed class Stream
    {
        internal string Signature = "";
        internal bool Le, Warning;
        internal double Last, Progress, WarmUntil, WindowAt, WindowPackets;
        internal int Bad, Good;
        internal long Packets;
        internal long[] Counters = [];
        internal double[] Damage = [];
    }
    private readonly Dictionary<string, Stream> streams = [];
    private string context = "";
    internal Func<double> Clock { get; set; } = () => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
    internal int Count => streams.Count;
    internal IEnumerable<string> WarningKeys => streams.Where(item => IsCurrentWarning(item.Value)).Select(item => item.Key);
    internal bool HasLeWarning => streams.Values.Any(value => value.Le && IsCurrentWarning(value));
    internal bool IsWarning(string key) => streams.TryGetValue(key, out var value) && IsCurrentWarning(value);
    private bool IsCurrentWarning(Stream value) { var age = Clock() - value.Last; return value.Warning && age >= 0 && age <= 4; }
    internal void Reset() => streams.Clear();
    internal void SetContext(string value) { if (context == value) return; context = value; Reset(); }
    internal void Remove(string key) => streams.Remove(key);
    internal void Retain(IEnumerable<string> keys)
    {
        var current = new HashSet<string>(keys);
        foreach (var key in streams.Keys.Where(key => !current.Contains(key)).ToArray()) streams.Remove(key);
    }
    private static bool Counter(JsonElement value, string name, out long count)
    {
        count = 0;
        return value.TryGetProperty(name, out var number) && number.ValueKind == JsonValueKind.Number && number.TryGetInt64(out count) && count >= 0;
    }
    private void Baseline(string key, string signature, bool le, double now, long packets, long[] counters)
    {
        if (!streams.ContainsKey(key) && streams.Count >= MaximumStreams)
            streams.Remove(streams.OrderBy(item => item.Value.Last).First().Key);
        streams[key] = new Stream { Signature = signature, Le = le, Last = now, Progress = now, WarmUntil = now + 2,
            WindowAt = now, Packets = packets, Counters = counters, Damage = new double[counters.Length] };
    }
    internal bool Observe(string key, JsonElement media, bool eligible = true)
    {
        if (key.Length == 0 || key.Length > 200) return false;
        var now = Clock();
        if (!eligible || media.ValueKind != JsonValueKind.Object || !Counter(media, "packets", out var packets)
            || media.Text("codec").Length == 0 || (!media.Flag("playing") && media.Number("packets_per_second") <= 0))
        { Remove(key); return false; }
        var le = media.Text("protocol") == "LE Audio" || media.Text("codec") == "lc3";
        var names = le ? LeCounters : ClassicCounters;
        var counters = new long[names.Length];
        var mask = 0;
        for (var i = 0; i < names.Length; i++) if (Counter(media, names[i], out counters[i])) mask |= 1 << i;
        if (mask == 0) { Remove(key); return false; }
        var signature = string.Join("/", media.Text("protocol", "A2DP"), media.Text("codec"), media.Number("rate"), media.Number("channels"), media.Number("depth"), mask);
        if (!streams.TryGetValue(key, out var value) || value.Signature != signature || now < value.Last || now - value.Last > 4
            || packets < value.Packets || counters.Where((count, i) => count < value.Counters[i]).Any())
        { Baseline(key, signature, le, now, packets, counters); return false; }
        if (now == value.Last) return IsCurrentWarning(value);
        var received = packets - value.Packets;
        value.Last = now;
        if (received > 0) value.Progress = now;
        if (now - value.Progress >= 1.5) { Remove(key); return false; }
        if (now <= value.WarmUntil)
        {
            value.WindowAt = now; value.Packets = packets; value.Counters = counters;
            return false;
        }
        value.WindowPackets += received;
        for (var i = 0; i < counters.Length; i++) value.Damage[i] += counters[i] - value.Counters[i];
        value.Packets = packets; value.Counters = counters;
        if (now - value.WindowAt >= 1)
        {
            var damaged = value.Damage.Max();
            var fraction = damaged / Math.Max(1, value.WindowPackets + damaged);
            var bad = value.WindowPackets >= 10 && damaged >= 3 && fraction >= .02;
            var good = value.WindowPackets >= 10 && (damaged <= 1 || fraction < .005);
            value.Bad = bad ? value.Bad + 1 : 0;
            value.Good = good ? value.Good + 1 : 0;
            if (value.Bad >= 3) value.Warning = true;
            else if (value.Good >= 5) value.Warning = false;
            value.WindowAt = now; value.WindowPackets = 0; Array.Clear(value.Damage, 0, value.Damage.Length);
        }
        return IsCurrentWarning(value);
    }
}

public partial class MainWindow
{
    internal MediaHealthTracker MediaHealth { get; } = new();
    private const string ClassicContinuityAction = "点“媒体编码”右侧齿轮，首选 AAC，或在 AAC 小齿轮降低码率；再在设备蓝牙设置中断开 BlueHead 并重连。";
    private const string LeContinuityAction = "点“媒体编码”右侧齿轮，改用“仅经典蓝牙”并首选 AAC；停止后重新开始接收，再在设备蓝牙设置中断开 BlueHead 并重连。";
    private void ObserveMediaHealth(string key, JsonElement stream)
    {
        var voice = stream.TryGetProperty("call", out var call) && call.Flag("active");
        if (settings.mode == "native" || voice || !stream.TryGetProperty("media", out var media)) { MediaHealth.Remove(key); return; }
        var audible = settings.media_mode != "single" || !media.TryGetProperty("selected", out _) || media.Flag("selected");
        MediaHealth.Observe(key, media, audible);
    }
}
