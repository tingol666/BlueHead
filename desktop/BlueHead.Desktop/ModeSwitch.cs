using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using BlueHead.Platform;

namespace BlueHead.Desktop;

// A mode choice is a preview until its driver requirements are met. Settings
// and the receiver always use the effective mode, never an unfinished draft.
public partial class MainWindow
{
    private string? modeDraft;
    private string? driverActivityOperation, usageOutcomeTitle, usageOutcomeDetail;
    private bool driverAwaitingPermission, usageOutcomeFailed;
    internal List<(string Operation, bool Waiting, string Title)> SelfTestDriverActivities { get; } = [];
    private static SolidColorBrush UsageBrush(string color) => new((Color)ColorConverter.ConvertFromString(color));
    private void SetUsageOutcome(string title, string detail, bool failed = false)
    {
        usageOutcomeTitle = title; usageOutcomeDetail = detail; usageOutcomeFailed = failed;
        UpdateModeChoice();
    }
    private void SetDriverActivity(string? operation, bool awaitingPermission = false)
    {
        driverActivityOperation = operation; driverAwaitingPermission = awaitingPermission;
        if (offline && operation != null) SelfTestDriverActivities.Add((operation, awaitingPermission, BluetoothSetup.DriverActivity(operation, awaitingPermission).Title));
        UpdateModeChoice();
    }
    internal string RequestedMode => modeDraft ?? settings.mode;
    internal bool HasPendingMode => modeDraft != null && modeDraft != settings.mode;
    private static string ModeName(string mode) => mode == "native" ? "基础模式" : "进阶模式";
    private bool CanUseMode(string mode)
    {
        if (!driversInspected || driverChecking || driverInventoryError != null) return false;
        var device = DriverPicker.SelectedItem as DriverDevice;
        return BluetoothSetup.ModeAvailable(mode, device, systemRadioReady);
    }
    private void CancelModeChoice()
    {
        modeDraft = null;
        var previous = applying; applying = true;
        SelectTag(ModePicker, settings.mode); SelectTag(HomeModePicker, settings.mode);
        applying = previous;
    }
    private async Task ChooseMode(string mode, bool fromHome)
    {
        if (running || stopping || starting || driverBusy || closing || driverChecking)
        { CancelModeChoice(); UpdateDriverActions(); return; }
        if (mode == settings.mode) { CancelModeChoice(); UpdateDriverActions(); return; }
        modeDraft = mode; DriverResult.Text = ""; usageOutcomeTitle = usageOutcomeDetail = null;
        var previous = applying; applying = true;
        SelectTag(ModePicker, settings.mode); SelectTag(HomeModePicker, settings.mode);
        applying = previous;
        if (CanUseMode(mode)) await CommitModeChoice();
        else
        {
            if (fromHome) ShowPage("adapters");
            UpdateDriverActions(); UpdateSetupState();
        }
    }
    private async Task CommitModeChoice()
    {
        if (!HasPendingMode || !CanUseMode(RequestedMode) || running || stopping || starting || driverBusy || closing) return;
        var selected = DriverPicker.SelectedItem as DriverDevice;
        string previousMode = settings.mode, desired = RequestedMode;
        if (desired != "native" && selected != null)
        {
            var choices = AdapterPicker.Items.OfType<AdapterChoice>().Where(choice => Drivers.MatchTransport(choice.Transport, new[] { selected }).Count == 1).ToArray();
            if (choices.Length == 1) { var previous = applying; applying = true; AdapterPicker.SelectedItem = choices[0]; applying = previous; settings.transport = choices[0].Transport; }
        }
        SetIntendedMode(desired);
        if (await Send(new { command = "configure", settings }))
        {
            DriverResult.Text = "已改用" + BluetoothSetup.UsageName(desired) + "。回音频中心点击“开始接收”。";
            SetUsageOutcome("已改用" + BluetoothSetup.UsageName(desired), "所需驱动已经就绪，无需再更换驱动。点击右上角“开始接收”即可使用。");
        }
        else
        {
            SetIntendedMode(previousMode); DriverResult.Text = "蓝牙使用方式未能保存。请先恢复后台连接，再重新选择。";
            SetUsageOutcome("使用方式未改变", DriverResult.Text, true);
        }
    }
    private async void HomeModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || HomeModePicker?.SelectedItem is not ComboBoxItem item) return;
        await ChooseMode(item.Tag?.ToString() ?? settings.mode, true);
    }
    private void CancelModeClick(object sender, RoutedEventArgs e) { CancelModeChoice(); UpdateDriverActions(); UpdateSetupState(); }
    private async void ApplyModeClick(object sender, RoutedEventArgs e) => await CommitModeChoice();
    private async void InstallDriverClick(object sender, RoutedEventArgs e) => await DriverOperation("install");
    private async void RestoreDriverClick(object sender, RoutedEventArgs e) => await DriverOperation("restore");
    private void UpdateModeChoice()
    {
        bool basic = settings.mode == "native";
        ModeDescription.Text = basic ? "基础模式：把设备的音乐、视频声音传到电脑。无需更换驱动，保留 Windows 蓝牙键盘、鼠标和耳机的连接。" : "进阶模式：接收设备声音，并发送电脑麦克风用于通话、录音或录像。支持媒体编码选择、独立音量、多设备同时连接和播放。";
        OtherModeDescription.Text = basic ? "基础模式的播放设备与音量在 Windows 调整。专用方式还支持电脑麦克风、媒体编码选择、多设备同时连接和播放。" : "只想听音乐、看视频？基础模式更省事；进阶模式需要切换所选设备的驱动。";
        var device = DriverPicker.SelectedItem as DriverDevice;
        var change = BluetoothSetup.UsageChange(RequestedMode);
        bool changing = driverActivityOperation != null;
        bool impactVisible = device != null && (RequestedMode != "native" && !BluetoothSetup.Dedicated(device) || HasPendingMode && RequestedMode == "native" && BluetoothSetup.Dedicated(device));
        DriverImpact.Visibility = !changing && impactVisible ? Visibility.Visible : Visibility.Collapsed;
        DriverImpactTitle.Text = change.ImpactTitle; DriverImpactText.Text = change.Impact;
        DriverImpact.Background = UsageBrush(change.Critical ? "#FFF1F2" : "#EDF7FB");
        DriverImpact.BorderBrush = UsageBrush(change.Critical ? "#EFCDD2" : "#C9E2EF");
        DriverImpactTitle.Foreground = DriverImpactText.Foreground = UsageBrush(change.Critical ? "#A73545" : "#2D6688");
        ModePendingNotice.Visibility = HasPendingMode && !changing ? Visibility.Visible : Visibility.Collapsed;
        ModePendingTitle.Text = change.Title;
        ModePendingText.Text = CanUseMode(RequestedMode) ? "驱动已经符合要求，请点击本页底部右侧的“使用" + ModeName(RequestedMode) + "”。返回或取消不会改动。" : change.Instruction;
        ChoiceFeedback.Visibility = HasPendingMode || changing ? Visibility.Visible : Visibility.Collapsed;
        ChoiceFeedback.Text = changing ? "驱动操作正在进行，进度见本页底部。" : "已选择“" + BluetoothSetup.UsageName(RequestedMode) + "”，尚未切换。下一步见本页底部。";
        DriverActivityNotice.Visibility = changing ? Visibility.Visible : Visibility.Collapsed;
        if (changing)
        {
            var activity = BluetoothSetup.DriverActivity(driverActivityOperation!, driverAwaitingPermission);
            DriverActivityTitle.Text = activity.Title; DriverActivityText.Text = activity.Detail;
            switch (driverActivityOperation)
            {
                case "install": DriverInstallButton.Content = driverAwaitingPermission ? "等待管理员授权…" : "正在备份并切换…"; break;
                case "restore": DriverRestoreButton.Content = driverAwaitingPermission ? "等待管理员授权…" : "正在恢复 Windows 蓝牙…"; break;
                default: OriginalBootstrapButton.Content = driverAwaitingPermission ? "等待管理员授权…" : "正在修复专用蓝牙…"; break;
            }
        }
        else
        {
            DriverInstallButton.Content = "备份并启用专用蓝牙";
            DriverRestoreButton.Content = "恢复 Windows 蓝牙";
            OriginalBootstrapButton.Content = "修复专用蓝牙";
        }
        HomeUsageNotice.Visibility = !running && usageOutcomeTitle != null ? Visibility.Visible : Visibility.Collapsed;
        HomeUsageTitle.Text = usageOutcomeTitle ?? ""; HomeUsageText.Text = usageOutcomeDetail ?? "";
        HomeUsageNotice.Background = UsageBrush(usageOutcomeFailed ? "#FFF1F2" : "#EAF7F0");
        HomeUsageTitle.Foreground = UsageBrush(usageOutcomeFailed ? "#A73545" : "#236544");
        CancelModeButton.Visibility = HasPendingMode ? Visibility.Visible : Visibility.Collapsed;
        CancelModeButton.IsEnabled = !driverBusy && !driverChecking && !running && !stopping && !starting;
        DriverStatus.Visibility = HasPendingMode || changing ? Visibility.Collapsed : Visibility.Visible;
        EffectiveModeLabel.Text = "当前方式：" + BluetoothSetup.UsageName(settings.mode);
        ApplyModeButton.Content = "使用" + ModeName(RequestedMode);
    }
}
