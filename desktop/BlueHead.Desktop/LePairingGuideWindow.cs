using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace BlueHead.Desktop;

internal sealed class LePairingGuideWindow : Window
{
    internal string GuideText { get; }
    internal LePairingGuideWindow(string policy, string name, string capability)
    {
        Title = "LE 配对指南 · BlueHead"; Width = 490; Height = 650;
        MaxHeight = Math.Max(360, SystemParameters.WorkArea.Height - 48);
        WindowStartupLocation = WindowStartupLocation.CenterOwner; ResizeMode = ResizeMode.NoResize;
        ShowInTaskbar = false; Background = new SolidColorBrush(Color.FromRgb(246, 249, 248));
        Foreground = (Brush)FindResource("Ink"); FontFamily = new FontFamily("Segoe UI, Microsoft YaHei UI");
        var root = new Grid { Margin = new Thickness(20, 16, 20, 16) };
        root.RowDefinitions.Add(new() { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new() { Height = GridLength.Auto });
        var panel = new StackPanel();
        var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = panel };
        root.Children.Add(scroll); Content = root;
        void Text(string text, bool title = false)
        {
            panel.Children.Add(new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap,
                FontSize = title ? 14 : 12, FontWeight = title ? FontWeights.SemiBold : FontWeights.Normal,
                Foreground = (Brush)FindResource(title ? "Ink" : "Muted"), Margin = new Thickness(0, title ? 12 : 5, 8, 0) });
        }
        Text("让设备通过 LE Audio 使用 LC3", true);
        Text("LC3 不在手机的经典蓝牙编码列表里。设备卡片实际显示“LE Audio / LC3”，才表示已使用这条音频链路。");
        Text("当前连接偏好：" + CodecPolicyWindow.PolicyLabel(policy));
        if (capability.Length > 0) Text(capability);
        Text("1  先让电脑只提供 LE Audio", true);
        Text("在首页“媒体编码”右侧点齿轮 →“连接与编码”→“连接偏好”，选择“仅 LE Audio / LC3”。回到首页点击“停止接收”，再点“开始接收”。需要 BlueHead 专用蓝牙。");
        Text("2  在手机或平板上重新配对", true);
        Text("打开设备的“设置 → 蓝牙”，进入已配对的“" + name + "”详情，点“取消配对／忽略设备”。返回可用设备列表刷新，再点这个名称重新配对。升级不会替你删除已有配对。");
        Text("3  打开 LE Audio 并播放音频", true);
        Text("在刚配对设备的详情页启用“LE Audio／低功耗音频”（如系统提供），再播放音乐。仅建立低功耗连接还不等于已建立 LC3 音频；软件会显示正在等待哪一步。");
        Text("仍然没有 LE Audio 选项？", true);
        Text("在 Android 的“开发者选项”中搜索“绕过蓝牙 LE 音频许可名单”（也可能叫“绕过 LE Audio 白名单”）。若有且可用，可开启后关闭并重新打开手机蓝牙，再按第 2 步重新配对。厂商可能隐藏、改名或禁用此开关。");
        Text("“停用蓝牙 LE 音频硬件分流／卸载”是另一项设置，不能代替许可名单开关。仅改电脑耳机名称也不能保证通过手机的型号检查。");
        Text("如何保留经典蓝牙兼容？", true);
        Text("双模“优先 LE Audio / LC3”同时提供两条链路，由发送设备选择。仅 LE Audio 不自动回退经典连接；不支持时可改回“仅经典蓝牙”。改变连接方式后需停止并重新开始接收。");
        GuideText = string.Join("\n", panel.Children.OfType<TextBlock>().Select(item => item.Text));
        var done = new Wpf.Ui.Controls.Button { Content = "知道了", HorizontalAlignment = HorizontalAlignment.Right,
            Padding = new Thickness(14, 5, 14, 5), Margin = new Thickness(0, 14, 0, 0), Appearance = Wpf.Ui.Controls.ControlAppearance.Primary };
        done.Click += (_, _) => Close(); Grid.SetRow(done, 1); root.Children.Add(done);
    }
}

public partial class MainWindow
{
    internal LePairingGuideWindow? PairingGuideWindow { get; private set; }
    internal void ShowLePairingGuide()
    {
        if (PairingGuideWindow != null) { PairingGuideWindow.Activate(); return; }
        var guide = new LePairingGuideWindow(CodecPolicyWindow.Policy(settings), settings.name, leCapabilitySummary) { Owner = PolicyWindow ?? (Window)this };
        PairingGuideWindow = guide;
        guide.Closed += (_, _) => { if (PairingGuideWindow == guide) PairingGuideWindow = null; };
        guide.Show();
    }
    private void LePairingGuideClick(object sender, RoutedEventArgs e) => ShowLePairingGuide();
}
