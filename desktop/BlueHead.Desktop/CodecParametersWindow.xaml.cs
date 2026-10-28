using System.Windows;
using System.Windows.Controls;

namespace BlueHead.Desktop;

public partial class CodecParametersWindow : Window
{
    private readonly string codec;
    private readonly Action<CodecParameters> save;
    internal CodecParametersWindow(string codec, CodecParameters parameters, Action<CodecParameters> save)
    {
        InitializeComponent(); this.codec = codec; this.save = save;
        MaxHeight = Math.Max(300, SystemParameters.WorkArea.Height - 48);
        CodecTitle.Text = CodecPolicyWindow.Codecs.Single(item => item.Code == codec).Name + " 参数";
        Title = CodecTitle.Text + " · BlueHead";
        Add(Rate, "自动 · 双方协商", 0);
        foreach (var rate in codec switch { "ldac" => new[] { 44100, 48000, 88200, 96000 }, "lhdc5" => new[] { 44100, 48000, 96000, 192000 }, _ => new[] { 44100, 48000 } })
            Add(Rate, $"{rate / 1000d:0.###} kHz", rate);
        Add(Depth, "自动", 0); Add(Depth, "16 bit", 16); Add(Depth, "24 bit", 24);
        if (codec == "lhdc5") { DepthRow.Visibility = Visibility.Visible; Grid.SetColumnSpan(RateRow, 1); }
        Add(Bitrate, "自动 · 最高 320 kb/s", 0);
        foreach (var bitrate in new[] { 128000, 192000, 256000, 320000 }) Add(Bitrate, $"{bitrate / 1000} kb/s", bitrate);
        if (codec == "aac") BitrateRow.Visibility = Visibility.Visible;
        Add(Quality, "自动 · 保留完整范围", "auto"); Add(Quality, "较高上限", "high");
        Add(Quality, "均衡上限", "balanced"); Add(Quality, "减少带宽占用", "bandwidth");
        if (codec is "sbc" or "aac") QualityRow.Visibility = Visibility.Visible;
        QualityReason.Text = codec switch
        {
            "aac" => "码率和质量均为协商上限，同时设置时采用较低上限；实际发送码率由设备选择。",
            "sbc" => "质量范围限制位池上限；实际码率还取决于发送设备选择的参数。",
            "ldac" or "lhdc5" => "发送码率及私有质量档位由发送设备决定，请在发送设备上调节。",
            _ => "此编码没有可单独协商的码率或质量档位，按双方能力协商。"
        };
        Select(Rate, parameters.rate); Select(Depth, parameters.depth); Select(Bitrate, parameters.bitrate); Select(Quality, parameters.quality);
    }
    private static void Add(ComboBox picker, string caption, object tag) => picker.Items.Add(new ComboBoxItem { Content = caption, Tag = tag.ToString() });
    private static void Select(ComboBox picker, object value) => picker.SelectedItem = picker.Items.OfType<ComboBoxItem>().FirstOrDefault(item => Equals(item.Tag, value.ToString())) ?? picker.Items[0];
    private static string Chosen(ComboBox picker) => (picker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "0";
    private static int Number(ComboBox picker) => int.TryParse(Chosen(picker), out var number) ? number : 0;
    private void ResetClick(object sender, RoutedEventArgs e) { foreach (var picker in new[] { Rate, Depth, Bitrate, Quality }) picker.SelectedIndex = 0; }
    private void SaveClick(object sender, RoutedEventArgs e)
    {
        save(new() { rate = Number(Rate), depth = codec == "lhdc5" ? Number(Depth) : 0,
            bitrate = codec == "aac" ? Number(Bitrate) : 0, quality = codec is "aac" or "sbc" ? Chosen(Quality) : "auto" });
        Close();
    }
}
