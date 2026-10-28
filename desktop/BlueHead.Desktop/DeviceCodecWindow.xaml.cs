using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace BlueHead.Desktop;

public partial class DeviceCodecWindow : Window
{
    internal readonly string Key;
    private readonly Func<object, Task<bool>> send;
    private readonly Func<int> nextRequest;
    private readonly Dictionary<string, JsonElement> capabilities = [];
    private readonly DispatcherTimer deadline = new();
    private bool applying, busy, connected = true, canAdjust;
    private int request;
    private string session = "";
    internal DeviceCodecWindow(string key, string name, Func<object, Task<bool>> send, Func<int> nextRequest)
    {
        InitializeComponent(); Key = key; DeviceTitle.Text = name; this.send = send; this.nextRequest = nextRequest;
        MaxHeight = Math.Max(300, SystemParameters.WorkArea.Height - 48);
        deadline.Tick += (_, _) => { deadline.Stop(); SetBusy(false); Status.Text = "设备暂未响应。若刚刚重新连接，请关闭本窗口，再从该设备的“…”重新打开。"; };
        Loaded += async (_, _) => await LoadOptions(); Closed += (_, _) => deadline.Stop();
    }
    private static string Chosen(ComboBox picker) => (picker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
    private static int Number(ComboBox picker) => int.TryParse(Chosen(picker), out var number) ? number : 0;
    private static void Add(ComboBox picker, string caption, object tag, bool enabled = true) => picker.Items.Add(new ComboBoxItem { Content = caption, Tag = tag.ToString(), IsEnabled = enabled });
    private static void Select(ComboBox picker, string value) => picker.SelectedItem = picker.Items.OfType<ComboBoxItem>().FirstOrDefault(item => Equals(item.Tag, value)) ?? picker.Items[0];
    private void SetBusy(bool value)
    {
        busy = value; Choices.IsEnabled = connected && !busy && canAdjust; Refresh.IsEnabled = connected && !busy;
        ApplyButton.IsEnabled = connected && !busy && canAdjust;
    }
    private async Task LoadOptions()
    {
        if (!connected || busy) return;
        SetBusy(true); Status.Text = "正在读取这台设备的真实能力…";
        request = nextRequest(); deadline.Interval = TimeSpan.FromSeconds(10); deadline.Start();
        if (!await send(new { command = "codec_options", key = Key, request_id = request })) { deadline.Stop(); SetBusy(false); Status.Text = "后台暂不可用，请重新开始接收后再打开。"; }
    }
    internal void Disconnected()
    {
        connected = false; deadline.Stop(); SetBusy(false);
        Status.Text = "这次连接已结束。重新连接后，请从设备的“…”重新打开编码与音质。";
    }
    internal void UpdateActual(JsonElement actual)
    {
        if (actual.ValueKind != JsonValueKind.Object) return;
        if (actual.Text("codec").Length == 0) return;
        var codec = actual.Text("codec");
        var name = CodecPolicyWindow.Codecs.FirstOrDefault(item => item.Code == codec).Name ?? codec.ToUpperInvariant();
        ActualFormat.Text = $"{(codec == "lc3" ? "LE Audio / " : "经典蓝牙 / ")}{name} · {actual.Number("rate") / 1000:0.###} kHz" + (actual.Number("depth") > 0 ? $" · {actual.Number("depth"):0} bit" : "")
            + (actual.TryGetProperty("bitrate", out _) ? $" · {actual.Number("bitrate") / 1000:0.#} kb/s" : "");
    }
    internal void UpdateTelemetry(JsonElement media) { if (connected && !busy) UpdateActual(media); }
    internal void Receive(JsonElement message)
    {
        if (!connected || message.Text("key") != Key) return;
        if (message.Text("event") == "codec_state")
        { if (message.TryGetProperty("actual", out var actual)) UpdateActual(actual); return; }
        if (message.Number("request_id") != request) return;
        if (message.Text("event") == "codec_options")
        {
            deadline.Stop(); capabilities.Clear(); canAdjust = false;
            if (!message.Flag("success")) { Status.Text = message.Text("message"); SetBusy(false); return; }
            if (message.TryGetProperty("actual", out var actual)) UpdateActual(actual);
            session = message.Text("session_id");
            Choices.Visibility = message.Text("audio_transport") == "le" ? Visibility.Collapsed : Visibility.Visible;
            applying = true; Codec.Items.Clear();
            Add(Codec, "跟随全局首选", "auto");
            if (message.TryGetProperty("codecs", out var rows) && rows.ValueKind == JsonValueKind.Array) foreach (var row in rows.EnumerateArray())
            {
                if (row.ValueKind != JsonValueKind.Object) continue;
                var code = row.Text("codec");
                if (!CodecPolicyWindow.Codecs.Any(item => item.Code == code)) continue;
                capabilities[code] = row.Clone();
                Add(Codec, row.Text("name") + (row.Flag("supported") ? "" : " · 对端未提供"), code, row.Flag("supported"));
            }
            var selection = message.TryGetProperty("selection", out var chosen) ? chosen : default;
            Select(Codec, selection.ValueKind == JsonValueKind.Object ? selection.Text("codec", "auto") : "auto");
            ConfigureFields();
            if (selection.ValueKind == JsonValueKind.Object)
            {
                Select(Rate, selection.Number("rate").ToString("0")); Select(Depth, selection.Number("depth").ToString("0"));
                Select(Bitrate, selection.Number("bitrate").ToString("0")); Select(Quality, selection.Text("quality", "auto"));
            }
            canAdjust = message.Text("audio_transport") == "classic" && session.Length == 24 && capabilities.Values.Any(row => row.Flag("supported"));
            applying = false; SetBusy(false);
            Status.Text = message.Text("reason") is { Length: > 0 } reason ? reason : "仅修改这一台设备；选择后点击“应用到此设备”。";
        }
        else if (message.Text("event") == "codec_switch")
        {
            Status.Text = message.Text("message");
            if (!message.Flag("done")) return;
            deadline.Stop(); SetBusy(false);
            if (message.TryGetProperty("actual", out var actual)) UpdateActual(actual);
        }
    }
    private void ConfigureFields()
    {
        var code = Chosen(Codec);
        capabilities.TryGetValue(code, out var row);
        bool exists = row.ValueKind == JsonValueKind.Object;
        foreach (var picker in new[] { Rate, Depth, Bitrate, Quality }) { picker.Items.Clear(); Add(picker, "自动", "0"); }
        Quality.Items.Clear(); Add(Quality, "自动 · 保留完整范围", "auto");
        if (exists)
        {
            if (row.TryGetProperty("rates", out var rates) && rates.ValueKind == JsonValueKind.Array)
                foreach (var rate in rates.EnumerateArray())
                    if (rate.ValueKind == JsonValueKind.Number && rate.TryGetInt32(out var number) && number is 44100 or 48000 or 88200 or 96000 or 192000)
                        Add(Rate, $"{number / 1000d:0.###} kHz", number);
            if (row.TryGetProperty("depths", out var depths) && depths.ValueKind == JsonValueKind.Array)
                foreach (var depth in depths.EnumerateArray())
                    if (depth.ValueKind == JsonValueKind.Number && depth.TryGetInt32(out var number) && number is 16 or 24)
                        Add(Depth, $"{number} bit", number);
        }
        foreach (int bitrate in new[] { 128000, 192000, 256000, 320000 }) Add(Bitrate, $"{bitrate / 1000} kb/s", bitrate);
        Add(Quality, "较高上限", "high"); Add(Quality, "均衡上限", "balanced"); Add(Quality, "减少带宽占用", "bandwidth");
        foreach (var picker in new[] { Rate, Depth, Bitrate, Quality }) picker.SelectedIndex = 0;
        Rate.IsEnabled = exists && row.Flag("supported") && Rate.Items.Count > 1;
        DepthRow.Visibility = code == "lhdc5" ? Visibility.Visible : Visibility.Collapsed;
        BitrateRow.Visibility = code == "aac" ? Visibility.Visible : Visibility.Collapsed;
        QualityRow.Visibility = exists && row.Flag("quality_writable") ? Visibility.Visible : Visibility.Collapsed;
        QualityReason.Text = exists ? row.Text("quality_reason") : "恢复全局首选，参数由双方能力自动协商。";
        CodecReason.Text = exists ? row.Text("reason") : "不改变全局设置，下次连接仍使用全局偏好。";
    }
    private void CodecChanged(object sender, SelectionChangedEventArgs e) { if (!applying) ConfigureFields(); }
    private async void RefreshClick(object sender, RoutedEventArgs e) => await LoadOptions();
    private async void ApplyClick(object sender, RoutedEventArgs e)
    {
        if (!connected || busy || !canAdjust) return;
        var code = Chosen(Codec);
        var selection = new { codec = code, rate = code == "auto" ? 0 : Number(Rate), depth = code == "lhdc5" ? Number(Depth) : 0,
            bitrate = code == "aac" ? Number(Bitrate) : 0, quality = code is "aac" or "sbc" ? Chosen(Quality) : "auto" };
        SetBusy(true); Status.Text = "正在核对并调整，声音会短暂中断…";
        request = nextRequest(); deadline.Interval = TimeSpan.FromSeconds(24); deadline.Start();
        if (!await send(new { command = "codec_switch", key = Key, request_id = request, session_id = session, selection }))
        { deadline.Stop(); SetBusy(false); Status.Text = "后台暂不可用，请重新开始接收后再调整。"; }
    }
}
