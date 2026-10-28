using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using BlueHead.Platform;

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
    private readonly Action<string, List<string>, string, Dictionary<string, CodecParameters>> changed;
    private readonly Dictionary<string, CheckBox> checks = [];
    private readonly Dictionary<string, TextBlock> summaries = [];
    private readonly Dictionary<string, Wpf.Ui.Controls.Button> gears = [];
    private readonly Dictionary<string, CodecParameters> parameters;
    private readonly HashSet<string> allowed;
    private readonly IReadOnlyDictionary<string, string> unavailable;
    private bool applying = true;
    private string preferred;
    internal CodecParametersWindow? ParametersWindow { get; private set; }
    internal CheckBox CheckFor(string codec) => checks[codec];
    internal Wpf.Ui.Controls.Button GearFor(string codec) => gears[codec];
    internal string SummaryFor(string codec) => summaries[codec].Text;
    internal CodecPolicyWindow(EngineSettings settings, IReadOnlyDictionary<string, string> unavailable, Action<string, List<string>, string, Dictionary<string, CodecParameters>> changed)
    {
        InitializeComponent(); this.changed = changed; this.unavailable = unavailable;
        allowed = Allowed(settings); preferred = Canonical(settings.codec); parameters = CodecParameters.CopyAll(settings.codec_parameters);
        MaxHeight = Math.Max(300, SystemParameters.WorkArea.Height - 48);
        PolicyPicker.SelectedIndex = settings.le_audio_enabled ? 0 : 1;
        foreach (var (code, name) in Codecs)
        {
            var check = new CheckBox { Content = name + (code == "sbc" ? " · 兼容必需" : unavailable.ContainsKey(code) ? " · 当前不可用" : ""), Tag = code,
                IsChecked = allowed.Contains(code), IsEnabled = code != "sbc" && !unavailable.ContainsKey(code), Margin = new(0, 1, 0, 1), Style = (Style)FindResource("CompactCodecCheck") };
            if (unavailable.TryGetValue(code, out var reason)) check.ToolTip = reason;
            check.Checked += CodecChanged; check.Unchecked += CodecChanged;
            var row = new Grid { Height = 30 };
            row.ColumnDefinitions.Add(new() { Width = new GridLength(1, GridUnitType.Star) }); row.ColumnDefinitions.Add(new() { Width = GridLength.Auto }); row.ColumnDefinitions.Add(new() { Width = GridLength.Auto });
            var summary = new TextBlock { FontSize = 11, Foreground = (Brush)FindResource("Muted"), VerticalAlignment = VerticalAlignment.Center, Margin = new(8, 0, 8, 0), MaxWidth = 195, TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap };
            Grid.SetColumn(summary, 1);
            var gear = new Wpf.Ui.Controls.Button { Content = new Viewbox { Width = 14, Height = 14, Child = new System.Windows.Shapes.Path { Data = (Geometry)FindResource("SettingsGearGeometry"), Fill = (Brush)FindResource("Accent") } },
                Width = 26, Height = 26, Padding = new Thickness(5), Background = Brushes.Transparent, BorderThickness = new Thickness(0), VerticalAlignment = VerticalAlignment.Center, Tag = code,
                ToolTip = new ToolTip { FontFamily = new FontFamily("Segoe UI, Microsoft YaHei UI"), Content = new TextBlock { Text = "设置 " + name + " 的采样率与可用参数", FontFamily = new FontFamily("Segoe UI, Microsoft YaHei UI") } } };
            System.Windows.Automation.AutomationProperties.SetName(gear, name + " 编码参数");
            gear.Click += (_, _) => OpenParameters(code); Grid.SetColumn(gear, 2);
            checks[code] = check; summaries[code] = summary; gears[code] = gear;
            row.Children.Add(check); row.Children.Add(summary); row.Children.Add(gear); CodecChecks.Children.Add(row);
        }
        Refresh(); applying = false;
    }
    private void Refresh()
    {
        var previous = applying; applying = true;
        PolicyExplanation.Text = PolicyPicker.SelectedIndex == 0
            ? "双方支持并开启 LE Audio 时使用 LC3；未建立 LE 音频时继续使用经典蓝牙。"
            : "关闭 LE Audio，使用下方双方支持的经典蓝牙媒体与语音。";
        foreach (var (code, name) in Codecs)
        {
            checks[code].Content = name + (code == "sbc" ? " · 兼容必需" : unavailable.ContainsKey(code) ? " · 当前不可用" : "");
            checks[code].IsEnabled = code != "sbc" && !unavailable.ContainsKey(code);
            checks[code].ToolTip = unavailable.TryGetValue(code, out var reason) ? reason : null;
            checks[code].Opacity = summaries[code].Opacity = allowed.Contains(code) ? 1 : .6;
            gears[code].IsEnabled = !unavailable.ContainsKey(code);
            var value = parameters.GetValueOrDefault(code) ?? new();
            var pieces = new List<string>();
            if (value.rate > 0) pieces.Add($"{value.rate / 1000d:0.###} kHz");
            if (value.depth > 0) pieces.Add($"{value.depth} bit");
            if (value.bitrate > 0) pieces.Add($"≤ {value.bitrate / 1000} kb/s");
            if (value.quality != "auto") pieces.Add(value.quality switch { "high" => "高上限", "balanced" => "均衡", _ => "省带宽" });
            summaries[code].Text = pieces.Count == 0 ? "自动" : string.Join(" · ", pieces);
            summaries[code].ToolTip = summaries[code].Text;
        }
        PreferencePicker.Items.Clear();
        PreferencePicker.Items.Add(new ComboBoxItem { Content = "自动 · 优先高清", Tag = "auto" });
        foreach (var (code, name) in Codecs.Where(item => allowed.Contains(item.Code)))
            PreferencePicker.Items.Add(new ComboBoxItem { Content = CodecBitrateLimits.Label(code), Tag = code, IsEnabled = !unavailable.ContainsKey(code),
                ToolTip = unavailable.TryGetValue(code, out var reason) ? reason : null });
        PreferencePicker.SelectedItem = PreferencePicker.Items.OfType<ComboBoxItem>().FirstOrDefault(item => Equals(item.Tag, preferred)) ?? PreferencePicker.Items[0];
        preferred = (PreferencePicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
        applying = previous;
    }
    internal void RefreshAvailability() => Refresh();
    internal void Synchronize(EngineSettings settings)
    {
        var next = Allowed(settings);
        var sameParameters = parameters.Count == settings.codec_parameters.Count && parameters.All(item => settings.codec_parameters.TryGetValue(item.Key, out var value) && item.Value.rate == value.rate && item.Value.depth == value.depth && item.Value.bitrate == value.bitrate && item.Value.quality == value.quality);
        if (allowed.SetEquals(next) && preferred == Canonical(settings.codec) && (PolicyPicker.SelectedIndex == 0) == settings.le_audio_enabled && sameParameters) return;
        applying = true;
        allowed.Clear(); allowed.UnionWith(next); preferred = Canonical(settings.codec);
        parameters.Clear(); foreach (var item in CodecParameters.CopyAll(settings.codec_parameters)) parameters[item.Key] = item.Value;
        foreach (var item in checks) item.Value.IsChecked = allowed.Contains(item.Key);
        PolicyPicker.SelectedIndex = settings.le_audio_enabled ? 0 : 1;
        Refresh(); applying = false;
    }
    internal void OpenParameters(string code)
    {
        if (unavailable.ContainsKey(code)) return;
        ParametersWindow?.Close();
        var editor = new CodecParametersWindow(code, parameters.GetValueOrDefault(code) ?? new(), value => { parameters[code] = value; Refresh(); Commit(); }) { Owner = this };
        ParametersWindow = editor;
        editor.Closed += (_, _) => { if (ParametersWindow == editor) ParametersWindow = null; };
        editor.Show();
    }
    private void Commit() { if (!applying) changed(PolicyPicker.SelectedIndex == 0 ? "prefer_le" : "classic", allowed.ToList(), preferred, CodecParameters.CopyAll(parameters)); }
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
