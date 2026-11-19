using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace BlueHead.Desktop;

public sealed record SharedSource(string Key, string Name, string State, string Detail, bool Selected = false, bool? WindowsConnected = null)
{
    public override string ToString() => Name + " · " + (WindowsConnected switch
    { true => "Windows 已连接", false => "Windows 未连接", _ => "Windows 状态待确认" }) + " · " + (State switch
    {
        "connected" => "音频已连接", "connecting" => "正在连接",
        "failed" => "需要处理", "waiting" => "等待音频连接", "selected" => "已记住选择",
        "blocked" => "暂时等待", _ => "未交给 BlueHead"
    });
}

public partial class MainWindow
{
    private bool updatingShared;
    private string? sharedModeSeen;
    private string? pendingSharedConnect;
    private List<SharedSource> sharedSources = [];
    private string? dedicatedIdentityHint;
    private Task<bool> ScanSharedSources() => Send(new { command = "shared_scan" });
    private static bool? WindowsConnected(JsonElement item) => item.TryGetProperty("windows_connected", out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False ? value.GetBoolean() : null;
    private void BindSharedSources(string? key = null)
    {
        key ??= (SharedSourcePicker.SelectedItem as SharedSource)?.Key;
        // Remembered choices stay reachable for revocation while disconnected.
        // The extra switch reveals only the remaining offline paired records.
        var visible = sharedSources.Where(item => item.WindowsConnected == true || item.Selected || SharedShowDisconnected.IsChecked == true)
            .OrderByDescending(item => item.WindowsConnected == true).ThenByDescending(item => item.Selected).ThenBy(item => item.Name).ToList();
        updatingShared = true;
        SharedSourcePicker.ItemsSource = visible;
        SharedSourcePicker.SelectedItem = visible.FirstOrDefault(item => item.Key == key) ?? visible.FirstOrDefault();
        updatingShared = false;
    }
    private void SharedFilterChanged(object sender, RoutedEventArgs e)
    { if (SharedSourcePicker == null) return; BindSharedSources(); UpdateSharedControls(); }

    private void UpdateSharedControls()
    {
        if (SharedPlaybackPanel == null) return;
        var shared = settings.mode == "native";
        SharedPlaybackPanel.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        if (!shared) { sharedModeSeen = null; pendingSharedConnect = null; return; }
        var scope = settings.mode;
        if (sharedModeSeen != scope)
        {
            sharedModeSeen = scope; sharedSources.Clear();
            SharedSourcePicker.ItemsSource = null;
            if (!offline) _ = ScanSharedSources();
        }
        var choice = SharedSourcePicker.SelectedItem as SharedSource;
        SharedConnectButton.Content = choice?.Selected == true || choice?.State == "connected" ? "停止接收此设备" : "接收此设备的音频";
        SharedConnectButton.IsEnabled = choice != null && choice.State != "connecting" && !starting && !stopping && !closing && !driverBusy && !driverChecking;
        SharedSourcePicker.IsEnabled = !starting && !stopping && !driverBusy;
        SharedRecheckButton.IsEnabled = !driverBusy && !driverChecking && !closing;
        SharedSelectionTitle.Text = "选择 Windows 已连接的设备";
        var chosen = sharedSources.Count(item => item.Selected);
        SharedSelectionCount.Text = $"已选择 {chosen} 台";
        SharedSelectionCount.ToolTip = "点击“接收此设备的音频”只打开你选择的设备；点击“停止接收此设备”保留 Windows 配对。播放设备与音量由 Windows 管理。";
        SharedPlaybackHint.Text = choice != null ? choice.Detail
            : sharedSources.Count == 0 ? "点击本卡片右上角“Windows 蓝牙设置”添加并连接设备，再点击“重新检测”。"
            : "当前没有 Windows 已连接的可用设备。请先在发送设备的蓝牙设置中连接这台电脑，再点击“重新检测”；也可勾选“显示未连接设备”，选择已配对设备准备音频服务。";
        // This panel already supplies the first-use steps and waiting state.
        EmptyState.Visibility = Visibility.Collapsed;
    }

    private bool ApplySharedMessage(string kind, JsonElement message)
    {
        if (kind == "pairing_required")
        {
            ShowNotice(message.Text("message"));
            UpdateSetupState();
            return true;
        }
        if (kind == "bluetooth_identity")
        {
            dedicatedIdentityHint = message.Flag("separated")
                ? $"专用蓝牙使用独立身份。首次使用时，在设备蓝牙列表选择“{settings.name}”并重新配对；Windows 配对保留。"
                : message.Text("reason") == "not_prepared"
                    ? "此适配器尚未准备独立身份。先停止接收，在“蓝牙驱动设置”点击“修复专用蓝牙”，再开始接收。"
                    : "此控制器暂未接受独立地址。两种方式可能仍显示为同一设备；切换后连接失败时，请忽略旧记录并重新配对。";
            if (settings.mode != "native") PairingHint.Text = dedicatedIdentityHint;
            return true;
        }
        if (kind is not ("shared_devices" or "shared_ready" or "shared_status")) return false;
        if (settings.mode != "native") return true;
        if (kind == "shared_devices")
        {
            // A stopped inventory request can finish after a new receiver starts.
            if (running && !message.Flag("running")) return true;
            var selected = (SharedSourcePicker.SelectedItem as SharedSource)?.Key;
            sharedSources = message.GetProperty("devices").EnumerateArray()
                .Select(item => new SharedSource(item.Text("key"), item.Text("name", "已配对设备"), item.Text("state"), item.Text("detail"), item.Flag("selected"), WindowsConnected(item))).ToList();
            BindSharedSources(selected);
        }
        else if (kind == "shared_status") SharedPlaybackHint.Text = message.Text("message");
        else if (pendingSharedConnect is { } key && running && !stopping)
        {
            pendingSharedConnect = null;
            _ = Send(new { command = "device_action", action = "shared_connect", key });
        }
        UpdateSharedControls();
        if (kind == "shared_status") SharedPlaybackHint.Text = message.Text("message");
        return true;
    }

    private void SharedSourceChanged(object sender, SelectionChangedEventArgs e)
    { if (!updatingShared) UpdateSharedControls(); }

    private async void SharedConnectClick(object sender, RoutedEventArgs e)
    {
        if (!SharedConnectButton.IsEnabled || SharedSourcePicker.SelectedItem is not SharedSource choice) return;
        if (choice.Selected || choice.State == "connected")
        { await Send(new { command = "device_action", action = "disconnect", key = choice.Key }); return; }
        if (!running)
        {
            pendingSharedConnect = choice.Key;
            await StartReception(automatic: false);
            if (!running) pendingSharedConnect = null;
        }
        else await Send(new { command = "device_action", action = "shared_connect", key = choice.Key });
    }

    private void ResetSharedSession()
    {
        pendingSharedConnect = null;
        var selected = (SharedSourcePicker.SelectedItem as SharedSource)?.Key;
        sharedSources = sharedSources.Select(item => item with { State = item.Selected ? "selected" : "available",
            Detail = item.Selected ? "已记住选择；开始接收后准备此设备的音频服务。Windows 配对保留。"
                : "Windows 管理；点击“接收此设备的音频”才交给 BlueHead。" }).ToList();
        BindSharedSources(selected);
        UpdateSharedControls();
    }
}
