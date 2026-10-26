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
    private readonly ComboBox picker = new() { DisplayMemberPath = "DisplayName", MinHeight = 38 };
    private readonly TextBlock status = new() { TextWrapping = TextWrapping.Wrap, Margin = new(0, 16, 0, 0) };
    private readonly Button restore = new() { Content = "恢复所选适配器", Margin = new(0, 20, 0, 0), Padding = new(18, 9, 18, 9), HorizontalAlignment = HorizontalAlignment.Left };
    internal RecoveryWindow()
    {
        Title = "BlueHead · 蓝牙恢复"; Width = 580; Height = 340; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        var panel = new StackPanel { Margin = new(28) };
        panel.Children.Add(new TextBlock { Text = "恢复系统蓝牙", FontSize = 24, FontWeight = FontWeights.SemiBold });
        panel.Children.Add(new TextBlock { Text = "此工具独立于蓝牙后台运行。请先退出接收程序，再恢复适配器。", Margin = new(0, 12, 0, 18), TextWrapping = TextWrapping.Wrap });
        panel.Children.Add(picker); panel.Children.Add(restore); panel.Children.Add(status); Content = panel;
        Loaded += async (_, _) => await Refresh(); restore.Click += Restore;
    }
    private async Task Refresh()
    {
        try { var items = await Task.Run(Drivers.Enumerate); picker.ItemsSource = items; picker.SelectedIndex = items.Count == 0 ? -1 : 0; status.Text = items.Count == 0 ? "未找到可恢复的蓝牙适配器。" : "恢复时 Windows 会请求管理员授权。"; }
        catch (Exception error) { status.Text = error.Message; }
    }
    private async void Restore(object sender, RoutedEventArgs e)
    {
        if (picker.SelectedItem is not DriverDevice device) return;
        if (MessageBox.Show(this, $"恢复 {device.Name} 的原蓝牙驱动？", "恢复系统蓝牙", MessageBoxButton.OKCancel) != MessageBoxResult.OK) return;
        restore.IsEnabled = false;
        status.Text = "正在恢复驱动并初始化系统蓝牙，请保持适配器连接…";
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "BlueHead.DriverHelper.exe");
            var start = new ProcessStartInfo(path) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
            start.AddArgument("--restore"); start.AddArgument(device.InstanceId);
            using var process = Process.Start(start) ?? throw new IOException("恢复工具未能启动。");
            await process.WaitForExitAsync();
            var record = Drivers.ReadRecord(device.InstanceId);
            await Refresh(); status.Text = process.ExitCode == 0 ? record?.RebootRequired == true ?
                "驱动恢复完成，请重启电脑。" : "系统蓝牙已恢复，初始化检查通过。" : record?.Error ?? "恢复未完成，请查看管理员窗口中的原因。";
        }
        catch (Exception error) { status.Text = error.Message; }
        finally { restore.IsEnabled = true; }
    }
}
