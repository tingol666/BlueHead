using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using BlueHead.Platform;
using Button = System.Windows.Controls.Button;
using MessageBox = System.Windows.MessageBox;

namespace BlueHead.Desktop;

internal sealed class RecoveryWindow : Window
{
    private bool busy;
    private readonly Button refresh = new() { Content = "刷新状态", Padding = new(8, 4, 8, 4), Margin = new(8, 0, 0, 0) };
    private readonly ComboBox picker = new() { DisplayMemberPath = "DisplayName", MinHeight = 38 };
    private readonly TextBlock status = new() { TextWrapping = TextWrapping.Wrap, Margin = new(0, 16, 0, 0) };
    private readonly Button restore = new() { Content = "恢复 Windows 蓝牙", Margin = new(0, 20, 0, 0), Padding = new(18, 9, 18, 9), HorizontalAlignment = HorizontalAlignment.Left };
    internal RecoveryWindow()
    {
        Title = "BlueHead · 独立蓝牙恢复"; Width = 640; Height = 430; MinWidth = 560; MinHeight = 350; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        var panel = new StackPanel { Margin = new(28) };
        panel.Children.Add(new TextBlock { Text = "恢复 Windows 蓝牙", FontSize = 24, FontWeight = FontWeights.SemiBold });
        panel.Children.Add(new TextBlock { Text = "主窗口无法使用，或 Windows 蓝牙开关消失时，可在这里恢复原厂驱动。\n1. 先在 BlueHead 音频中心点击“停止接收”，或在系统托盘右键 BlueHead →“退出”。\n2. 在下方选择电脑的蓝牙设备，再点击“恢复 Windows 蓝牙”。允许 Windows 的管理员授权。", Margin = new(0, 12, 0, 18), TextWrapping = TextWrapping.Wrap });
        var selection = new DockPanel(); DockPanel.SetDock(refresh, Dock.Right); selection.Children.Add(refresh); selection.Children.Add(picker);
        refresh.Click += async (_, _) => await Refresh();
        panel.Children.Add(selection); panel.Children.Add(restore); panel.Children.Add(status);
        var windows = new Button { Content = "打开 Windows 蓝牙 ↗", Padding = new(8, 4, 8, 4), Margin = new(0, 14, 0, 0), HorizontalAlignment = HorizontalAlignment.Left };
        windows.Click += (_, _) => { try { Process.Start(new ProcessStartInfo("ms-settings:bluetooth") { UseShellExecute = true }); } catch { status.Text = "无法打开 Windows 设置。请在 Windows 开始菜单打开“设置”，搜索“蓝牙”。"; } };
        panel.Children.Add(windows); Content = new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Closing += (_, e) => { if (busy) { e.Cancel = true; status.Text = "驱动恢复正在进行。请等待完成后再关闭窗口，不要拔出蓝牙设备。"; } };
        Loaded += async (_, _) => await Refresh(); restore.Click += Restore; picker.SelectionChanged += (_, _) => UpdateState();
    }
    private void UpdateState()
    {
        var guide = BluetoothSetup.Adapter(picker.SelectedItem as DriverDevice);
        restore.Content = "恢复 Windows 蓝牙";
        restore.IsEnabled = !busy && guide.CanRestore;
        status.Text = guide.State + "\n" + guide.Detail;
        if (picker.SelectedItem is DriverDevice device && BluetoothSetup.Interrupted(device.RecoveryStage))
            status.Text += "\n上次准备未完成；原厂备份保留，可以恢复系统蓝牙后继续。";
    }
    private async Task Refresh()
    {
        if (busy) return;
        refresh.IsEnabled = false;
        try
        {
            var selected = (picker.SelectedItem as DriverDevice)?.InstanceId;
            var items = await Task.Run(Drivers.Enumerate);
            picker.ItemsSource = items;
            picker.SelectedItem = items.FirstOrDefault(device => device.InstanceId == selected) ?? items.FirstOrDefault(device => BluetoothSetup.Adapter(device).CanRestore) ?? items.FirstOrDefault();
            UpdateState();
        }
        catch { restore.IsEnabled = false; status.Text = "没有读取到蓝牙状态。确认设备已连接，再点击本窗口右上方的“刷新状态”；仍失败时，重启电脑后重试。"; }
        finally { refresh.IsEnabled = !busy; }
    }
    private async void Restore(object sender, RoutedEventArgs e)
    {
        if (busy || picker.SelectedItem is not DriverDevice device || !BluetoothSetup.Adapter(device).CanRestore) return;
        busy = true; restore.IsEnabled = false; picker.IsEnabled = false; refresh.IsEnabled = false;
        status.Text = "正在恢复驱动并初始化系统蓝牙，请保持适配器连接…";
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "BlueHead.DriverHelper.exe");
            var start = new ProcessStartInfo(path) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
            start.AddArgument("--restore"); start.AddArgument(device.InstanceId);
            using var process = Process.Start(start) ?? throw new IOException("恢复工具未能启动。");
            await process.WaitForExitAsync();
            var record = Drivers.ReadRecord(device.InstanceId);
            busy = false; await Refresh(); busy = true;
            var feedback = BluetoothSetup.DriverCompletion("restore", process.ExitCode, record);
            status.Text = feedback.Ready ? "Windows 蓝牙已恢复。点击本窗口下方“打开 Windows 蓝牙”，重新连接键盘、鼠标或耳机；也可回 BlueHead 选择基础模式收听媒体。" : feedback.Message;
        }
        catch (Win32Exception error) when (error.NativeErrorCode == 1223) { status.Text = "已取消恢复，驱动未因这次操作而改变。需要时可重新点击“恢复 Windows 蓝牙”。"; }
        catch (Exception error) { status.Text = BluetoothSetup.DriverFailure("restore", error.Message); }
        finally { busy = false; picker.IsEnabled = true; refresh.IsEnabled = true; restore.IsEnabled = BluetoothSetup.Adapter(picker.SelectedItem as DriverDevice).CanRestore; }
    }
}
