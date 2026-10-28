using System.Windows;
using System.Windows.Controls;

namespace BlueHead.Desktop;

public partial class CodecPolicyWindow : Window
{
    internal static readonly (string Code, string Name)[] Codecs = [("lhdc5", "LHDC 5.0"), ("ldac", "LDAC"), ("aptx_hd", "aptX HD"), ("aptx", "aptX"), ("aac", "AAC"), ("sbc", "SBC")];
    internal static string Canonical(string codec) => codec == "aac_192" ? "aac" : codec;
    internal static HashSet<string> Allowed(EngineSettings settings)
    {
        var allowed = settings.allowed_codecs == null ? new HashSet<string>(new[] { "sbc", "aac", "aptx", "aptx_hd" }) : new HashSet<string>(settings.allowed_codecs);
        if (settings.allowed_codecs == null) { if (settings.ldac_enabled) allowed.Add("ldac"); if (settings.lhdc5_enabled) allowed.Add("lhdc5"); }
        allowed.Add("sbc"); return allowed;
    }
    private readonly Action<string, List<string>, string> changed;
    private readonly Dictionary<string, CheckBox> checks = [];
    private readonly HashSet<string> allowed;
    private readonly IReadOnlyDictionary<string, string> unavailable;
    private bool applying = true;
    private string preferred;
    internal CodecPolicyWindow(EngineSettings settings, IReadOnlyDictionary<string, string> unavailable, Action<string, List<string>, string> changed)
    {
        InitializeComponent(); this.changed = changed; this.unavailable = unavailable;
        allowed = Allowed(settings); preferred = settings.codec;
        MaxHeight = Math.Max(300, SystemParameters.WorkArea.Height - 48);
        PolicyPicker.SelectedIndex = settings.le_audio_enabled ? 0 : 1;
        foreach (var (code, name) in Codecs)
        {
            var check = new CheckBox { Content = name + (code == "sbc" ? " · 兼容必需" : unavailable.ContainsKey(code) ? " · 当前不可用" : ""), Tag = code,
                IsChecked = allowed.Contains(code), IsEnabled = code != "sbc" && !unavailable.ContainsKey(code), Margin = new(0, 1, 0, 1), Style = (Style)FindResource("CompactCodecCheck") };
            if (unavailable.TryGetValue(code, out var reason)) check.ToolTip = reason;
            check.Checked += CodecChanged; check.Unchecked += CodecChanged;
            checks[code] = check; CodecChecks.Children.Add(check);
            if (unavailable.TryGetValue(code, out reason)) CodecChecks.Children.Add(new TextBlock { Text = reason, FontSize = 11, Foreground = (System.Windows.Media.Brush)FindResource("Muted"), Margin = new(24, 0, 0, 4) });
        }
        Refresh(); applying = false;
    }
    private void Refresh()
    {
        var previous = applying; applying = true;
        PolicyExplanation.Text = PolicyPicker.SelectedIndex == 0
            ? "优先使用 LC3。需要电脑蓝牙与发送设备都支持 LE Audio，发送设备也须开启此功能。未建立 LE 音频时继续使用经典蓝牙；不会把经典连接显示成 LC3。"
            : "只提供经典蓝牙媒体与语音，不开启 LE Audio。适合兼容性排查；仍可使用下方双方支持的高清编码。";
        foreach (var item in checks) item.Value.Opacity = allowed.Contains(item.Key) ? 1 : .6;
        PreferencePicker.Items.Clear();
        PreferencePicker.Items.Add(new ComboBoxItem { Content = "自动 · 优先高清", Tag = "auto" });
        foreach (var (code, name) in Codecs.Where(item => allowed.Contains(item.Code)))
            PreferencePicker.Items.Add(new ComboBoxItem { Content = name, Tag = code, IsEnabled = !unavailable.ContainsKey(code) });
        if (allowed.Contains("aac")) PreferencePicker.Items.Add(new ComboBoxItem { Content = "AAC · 上限 192 kb/s", Tag = "aac_192", IsEnabled = !unavailable.ContainsKey("aac") });
        PreferencePicker.SelectedItem = PreferencePicker.Items.OfType<ComboBoxItem>().FirstOrDefault(item => Equals(item.Tag, preferred)) ?? PreferencePicker.Items[0];
        preferred = (PreferencePicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
        applying = previous;
    }
    private void Commit() { if (!applying) changed(PolicyPicker.SelectedIndex == 0 ? "prefer_le" : "classic", allowed.ToList(), preferred); }
    private void PolicyChanged(object sender, SelectionChangedEventArgs e) { if (applying) return; Refresh(); Commit(); }
    private void PreferenceChanged(object sender, SelectionChangedEventArgs e) { if (applying) return; preferred = (PreferencePicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto"; Commit(); }
    private void CodecChanged(object sender, RoutedEventArgs e)
    {
        if (applying || sender is not CheckBox { Tag: string code } check) return;
        if (check.IsChecked == true) allowed.Add(code); else allowed.Remove(code);
        if (preferred != "auto" && !allowed.Contains(Canonical(preferred))) preferred = "auto";
        Refresh(); Commit();
    }
    private void DoneClick(object sender, RoutedEventArgs e) => Close();
}
