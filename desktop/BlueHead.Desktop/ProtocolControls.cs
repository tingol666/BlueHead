using System.Text.Json;
using System.Windows;

namespace BlueHead.Desktop;

public partial class MainWindow
{
    private const string MediaOnlyNotice = "此适配器当前仅支持媒体接收；电脑麦克风与混音不可用。媒体编码和音量设置保留。";
    private bool broadcastAvailable, protocolCapabilityNotice;
    private string leCapabilitySummary = "";
    private void BroadcastClick(object sender, RoutedEventArgs e) => ShowPage("broadcast");
    private void InitializeProtocolControls()
    {
        BroadcastPage.Send = Send;
        BroadcastPage.OpenSettings = () =>
        {
            if (settings.mode == "native" || (running && settings.le_audio_enabled && leCapabilitySummary.Length > 0 && !broadcastAvailable)) ShowPage("adapters");
            else if (!settings.le_audio_enabled) ShowAdvancedAudio();
            else ShowPage("home");
        };
    }
    private void ShowProtocolCapability(string text)
    {
        ShowNotice(text); protocolCapabilityNotice = true;
    }
    private void UpdateProtocolAvailability()
    {
        LeAudioState.Text = !settings.le_audio_enabled ? "开启后保存设置，再点击音频中心的“开始接收”。" : !running ? "点击音频中心的“开始接收”后，这里显示适配器能力。" : leCapabilitySummary.Length > 0 ? leCapabilitySummary : "正在检查适配器的 LE Audio 能力…";
        string reason, action;
        bool actionable = !stopping && !driverBusy;
        if (settings.mode == "native")
        {
            reason = "音频广播需要进阶模式。请在音频中心将“蓝牙使用方式”改为 BlueHead 专用蓝牙，并按蓝牙驱动设置页的步骤完成切换。";
            action = "打开蓝牙驱动设置 ↗";
        }
        else if (!settings.le_audio_enabled)
        {
            reason = "请在“设置 → 高级音频设置”开启 LE Audio，点击“保存设置”，再到音频中心点击“开始接收”。";
            action = "打开高级音频设置 ↗";
        }
        else if (!running)
        {
            reason = "请先在音频中心点击“开始接收”，软件随后检查这个适配器的广播接收能力。";
            action = "返回音频中心 ↗";
        }
        else if (leCapabilitySummary.Length == 0)
        {
            reason = "正在检查这个蓝牙适配器的广播接收能力，请等待接收状态就绪。";
            action = "正在检查蓝牙能力…"; actionable = false;
        }
        else
        {
            reason = "当前适配器未提供广播接收能力。可继续使用经典蓝牙音频；如需广播，请先停止接收，再在蓝牙驱动设置中换用支持对应 LE Audio 功能的适配器。";
            action = "查看蓝牙设备 ↗";
        }
        BroadcastPage.SetAvailability(running && !stopping && !driverBusy && settings.mode != "native" && settings.le_audio_enabled && broadcastAvailable, reason, action);
        BroadcastPage.SettingsButton.IsEnabled = actionable;
    }
    private void ApplyProtocolMessage(JsonElement message)
    {
        string kind = message.Text("event");
        if (kind is "starting" or "stopping" or "stopped")
        {
            broadcastAvailable = false; leCapabilitySummary = ""; protocolCapabilityNotice = false;
            UpdateProtocolAvailability();
        }
        if (kind == "le_audio_capabilities")
        {
            broadcastAvailable = message.Flag("broadcast");
            leCapabilitySummary = $"此适配器：媒体接收{(message.Flag("receive") ? "可用" : "不可用")} · 麦克风发送{(message.Flag("transmit") ? "可用" : "不可用")} · 广播接收{(broadcastAvailable ? "可用" : "不可用")}。";
            if (message.Flag("transmit"))
            {
                sessionMediaOnly = false;
                if (protocolCapabilityNotice) { Notice.Visibility = Visibility.Collapsed; protocolCapabilityNotice = false; }
                ApplySettings();
            }
            else if (sessionMediaOnly && protocolCapabilityNotice) ShowProtocolCapability(MediaOnlyNotice);
            UpdateProtocolAvailability();
        }
        if (kind == "broadcast") BroadcastPage.Apply(message);
        else if (kind == "telemetry" && message.TryGetProperty("broadcast", out var snapshot)) BroadcastPage.Apply(snapshot);
    }
}
