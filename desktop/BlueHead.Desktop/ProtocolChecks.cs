using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void ProtocolChecks(MainWindow window, Dictionary<string, bool> checks, string folder, Action<string> receive)
    {
        void Click(System.Windows.Controls.Primitives.ButtonBase button) { button.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent)); Pump(); }
        IEnumerable<T> Visuals<T>(DependencyObject parent) where T : DependencyObject
        {
            for (var i=0;i<System.Windows.Media.VisualTreeHelper.GetChildrenCount(parent);i++)
            {
                var child=System.Windows.Media.VisualTreeHelper.GetChild(parent,i);
                if(child is T match) yield return match;
                foreach(var nested in Visuals<T>(child)) yield return nested;
            }
        }
        receive("""{"event":"stopped"}""");
        window.ShowPage("settings"); window.LeAudioToggle.IsChecked = true; window.Lhdc5Toggle.IsChecked = true;
        Click(window.SaveButton); Pump();
        var saved = window.SelfTestCommands.Last(item => item.Text("command") == "configure").GetProperty("settings");
        checks["le_audio_lhdc_settings_persist_without_restarting"] = saved.Flag("le_audio_enabled") && saved.Flag("lhdc5_enabled") && window.StartButton.Content.ToString() == "开始接收";
        window.ShowPage("broadcast"); Pump();
        checks["broadcast_page_reachable_and_explains_start"] = window.BroadcastPage.IsVisible && window.BroadcastPage.Guidance.Text.Contains("开始接收") && !window.BroadcastPage.ScanButton.IsEnabled;
        checks["broadcast_start_guidance_returns_to_audio_center"] = Equals(window.BroadcastPage.SettingsButton.Content, "返回音频中心 ↗");
        Click(window.BroadcastPage.SettingsButton); checks["broadcast_start_guidance_opens_home"] = window.HomePage.IsVisible;
        window.ShowPage("broadcast");
        receive("""{"event":"starting"}""");
        receive("""{"event":"controller_capabilities","voice_transport":false,"le_audio_pending":true}""");
        checks["le_pending_does_not_prematurely_claim_no_microphone"] = window.NoticeText.Text.Contains("正在检查 LE Audio");
        receive("""{"event":"le_audio_capabilities","receive":true,"transmit":true,"broadcast":true}"""); Pump();
        checks["le_capabilities_are_visible_beside_protocol_setting"] = window.LeAudioState.Text.Contains("媒体接收可用") && window.LeAudioState.Text.Contains("麦克风发送可用") && window.LeAudioState.Text.Contains("广播接收可用");
        checks["native_le_voice_enables_microphone_on_non_sco_transport"] = window.InputPicker.IsEnabled && window.MicrophoneGain.IsEnabled && window.Notice.Visibility != Visibility.Visible;
        Click(window.BroadcastPage.ScanButton);
        checks["broadcast_scan_uses_existing_engine_command"] = window.SelfTestCommands.Last().Text("command") == "broadcast" && window.SelfTestCommands.Last().Text("operation") == "scan" && window.SelfTestCommands.Last().Flag("enabled");
        receive("""{"event":"broadcast","scanning":true,"sources":[{"address":"12:34:56:78:9A:BC","address_type":0,"sid":3,"broadcast_id":42,"name":"广播示例","rssi":-40}],"active":{"state":"code_required","message":"这个广播需要密码","subgroup":0},"programs":[{"index":0,"title":"中文节目"},{"index":1,"title":"English"}]}"""); Pump();
        checks["broadcast_password_program_and_scan_are_visible"] = window.BroadcastPage.PasswordRow.IsVisible && window.BroadcastPage.ProgramsRow.IsVisible && window.BroadcastPage.Sources.Items.Count == 1 && Equals(window.BroadcastPage.ScanButton.Content,"停止搜索");
        receive("""{"event":"telemetry","audio":{},"devices":[],"devices_complete":false,"broadcast":{"scanning":true,"active":{"state":"code_required","message":"这个广播需要密码","subgroup":0},"programs":[{"index":0,"title":"中文节目"},{"index":1,"title":"English"}],"audio":{"rate":48000,"bitrate":192000}}}"""); Pump();
        checks["broadcast_shows_actual_protocol_rate_and_live_bitrate"] = window.BroadcastPage.AudioStatus.IsVisible && window.BroadcastPage.AudioStatus.Text.Contains("LC3 · 48 kHz · 192 kb/s");
        window.BroadcastPage.Password.Password="secret-demo";
        var submit = Visuals<Wpf.Ui.Controls.Button>(window.BroadcastPage.PasswordRow).Single(); Click(submit);
        checks["broadcast_password_is_sent_once_and_cleared"] = window.BroadcastPage.Password.Password.Length == 0 && window.SelfTestCommands.Last().Text("code") == "secret-demo";
        Capture(window, Path.Combine(folder,"broadcast-password-programs.png"));
        receive("""{"event":"device","key":"le-demo","state":"已连接"}""");
        receive("""{"event":"telemetry","audio":{},"devices":[{"key":"le-demo","media":{"protocol":"LE Audio","codec":"lc3","rate":48000,"bitrate":192000,"playing":true,"presentation_ms":40,"plc_frames":2},"call":{}}]}""");
        var card=window.State.Devices.Single();
        checks["le_codec_and_details_do_not_claim_classic_rtp"] = card.MediaDescription.Contains("LE Audio / LC3") && card.LiveDetails.Contains("40 ms") && !card.LiveDetails.Contains("RTP");
        receive("""{"event":"telemetry","audio":{},"devices":[{"key":"le-demo","media":{},"call":{"active":true,"protocol":"LE Audio","codec":"lc3","rate":32000,"rx_bitrate":64000,"tx_bitrate":64000,"microphone_active":true}}]}""");
        checks["le_duplex_label_distinguishes_hfp_lc3_swb"] = card.MediaDescription.Contains("LE Audio / LC3 · 32 kHz") && !card.MediaDescription.Contains("HFP");
        receive("""{"event":"stopping"}"""); Pump();
        checks["stopping_disables_broadcast_and_erases_password"] = !window.BroadcastPage.ScanButton.IsEnabled && window.BroadcastPage.Password.Password.Length == 0;
        receive("""{"event":"stopped"}""");
        window.ShowPage("home");
        window.CodecPicker.SelectedItem=window.CodecPicker.Items.OfType<ComboBoxItem>().Single(item=>Equals(item.Tag,"lhdc5"));
        window.ApplyCodecProfile().GetAwaiter().GetResult();
        checks["home_lhdc_selection_enables_decoder_without_losing_le_setting"] = window.SelfTestCommands.Last().GetProperty("settings").Text("codec")=="lhdc5" && window.SelfTestCommands.Last().GetProperty("settings").Flag("lhdc5_enabled") && window.SelfTestCommands.Last().GetProperty("settings").Flag("le_audio_enabled");
    }
}
