using System.Collections.ObjectModel;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace BlueHead.Desktop;

public sealed class BroadcastChoice
{
    public string Address { get; init; } = "";
    public int AddressType { get; init; }
    public int Sid { get; init; }
    public int BroadcastId { get; init; }
    public string Name { get; init; } = "音频广播";
    public int Rssi { get; init; }
    public string Key => $"{Address}|{AddressType}|{Sid}|{BroadcastId}";
    public string Details => $"音频广播 · 信号 {Rssi} dBm";
}
public sealed class BroadcastProgram
{
    public int Index { get; init; }
    public string Title { get; init; } = "";
}
public partial class BroadcastPanel : UserControl
{
    private readonly ObservableCollection<BroadcastChoice> sources = [];
    private bool scanning, applying, available;
    public Func<object, Task<bool>>? Send { get; set; }
    public Action? OpenSettings { get; set; }
    public BroadcastPanel()
    {
        InitializeComponent(); Sources.ItemsSource = sources;
    }
    public void SetAvailability(bool enabled, string reason, string action = "打开高级音频设置 ↗")
    {
        available = enabled; ScanButton.IsEnabled = enabled; SettingsButton.Content = action;
        SettingsButton.Visibility = enabled ? Visibility.Collapsed : Visibility.Visible;
        if (enabled) { if (sources.Count == 0) Guidance.Text = "点击“搜索附近广播”，再选择要在电脑播放的广播。"; return; }
        scanning = false; ScanButton.Content = "搜索附近广播";
        Status.Text = "音频广播尚未就绪"; Guidance.Text = reason; AudioStatus.Visibility = Visibility.Collapsed;
        sources.Clear(); Empty.Visibility = Visibility.Visible;
        Password.Password = ""; PasswordRow.Visibility = ProgramsRow.Visibility = Visibility.Collapsed;
        StopButton.IsEnabled = BroadcastVolume.CanAdjust = false;
    }
    public void Apply(JsonElement snapshot)
    {
        if (!available || snapshot.ValueKind != JsonValueKind.Object) return;
        applying = true;
        try
        {
            AudioStatus.Visibility = Visibility.Collapsed;
            if (snapshot.TryGetProperty("audio", out var audio) && audio.ValueKind == JsonValueKind.Object && audio.Number("rate") > 0)
            {
                AudioStatus.Text = $"LE Audio / LC3 · {audio.Number("rate") / 1000:0.###} kHz · {audio.Number("bitrate") / 1000:0.#} kb/s";
                AudioStatus.Visibility = Visibility.Visible;
            }
            BroadcastVolume.Value = (snapshot.TryGetProperty("gain", out _) ? snapshot.Number("gain") : 1) * 100;
            scanning = snapshot.Flag("scanning"); ScanButton.Content = scanning ? "停止搜索" : "搜索附近广播";
            var selected = (Sources.SelectedItem as BroadcastChoice)?.Key;
            if (snapshot.TryGetProperty("sources", out var found) && found.ValueKind == JsonValueKind.Array)
            {
                sources.Clear();
                foreach (var item in found.EnumerateArray().Take(64))
                    sources.Add(new() { Address = item.Text("address"), AddressType = (int)item.Number("address_type"), Sid = (int)item.Number("sid"), BroadcastId = (int)item.Number("broadcast_id"), Name = item.Text("name", "音频广播"), Rssi = (int)item.Number("rssi") });
                Sources.SelectedItem = sources.FirstOrDefault(item => item.Key == selected);
            }
            Empty.Visibility = sources.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            Empty.Text = scanning ? "正在搜索，请在发送设备上开启音频分享或广播。" : "搜索后，附近的广播会显示在这里。";
            if (snapshot.TryGetProperty("active", out var active) && active.ValueKind == JsonValueKind.Object)
            {
                var state = active.Text("state");
                Status.Text = state == "playing" ? "正在播放音频广播" : state is "code_required" or "bad_code" ? "需要广播密码" : state == "failed" ? "广播连接未完成" : "正在连接音频广播";
                Guidance.Text = active.Text("message"); StopButton.IsEnabled = true;
                PasswordRow.Visibility = state is "code_required" or "bad_code" ? Visibility.Visible : Visibility.Collapsed;
                BroadcastVolume.CanAdjust = state == "playing";
                var programs = new List<BroadcastProgram>();
                if (snapshot.TryGetProperty("programs", out var choices) && choices.ValueKind == JsonValueKind.Array)
                    foreach (var item in choices.EnumerateArray().Take(31)) programs.Add(new() { Index = (int)item.Number("index"), Title = item.Text("title") });
                Programs.ItemsSource = programs;
                Programs.SelectedItem = programs.FirstOrDefault(item => item.Index == (int)active.Number("subgroup"));
                ProgramsRow.Visibility = programs.Count > 1 ? Visibility.Visible : Visibility.Collapsed;
            }
            else
            {
                Status.Text = "尚未连接音频广播"; Guidance.Text = "点击一个广播的“播放”，即可通过当前电脑播放设备收听。";
                StopButton.IsEnabled = BroadcastVolume.CanAdjust = false;
                PasswordRow.Visibility = ProgramsRow.Visibility = Visibility.Collapsed;
            }
        }
        finally { applying = false; }
    }
    private async Task Submit(object message) { if (Send != null) await Send(message); }
    private async void ScanClick(object sender, RoutedEventArgs e) => await Submit(new { command = "broadcast", operation = "scan", enabled = !scanning });
    private async void StopClick(object sender, RoutedEventArgs e) => await Submit(new { command = "broadcast", operation = "stop" });
    private void SettingsClick(object sender, RoutedEventArgs e) => OpenSettings?.Invoke();
    private async void PlayClick(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: BroadcastChoice item })
            await Submit(new { command = "broadcast", operation = "select", address = item.Address, address_type = item.AddressType, sid = item.Sid, broadcast_id = item.BroadcastId });
    }
    private async void PasswordClick(object sender, RoutedEventArgs e)
    {
        var code = Password.Password;
        if (code.Length == 0 || Encoding.UTF8.GetByteCount(code) > 16) { Guidance.Text = "请按发送设备显示的密码输入（最多16个英文字母或数字）。"; return; }
        Password.Password = "";
        await Submit(new { command = "broadcast", operation = "code", code });
    }
    private async void ProgramChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!applying && Programs.SelectedItem is BroadcastProgram program)
            await Submit(new { command = "broadcast", operation = "program", index = program.Index });
    }
    private async void VolumeChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (!applying && available && BroadcastVolume.CanAdjust)
            await Submit(new { command = "broadcast", operation = "gain", gain = e.NewValue / 100 });
    }
}
