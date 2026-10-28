using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void CodecChecks(MainWindow window, Dictionary<string, bool> checks, string folder, Action<string> receive)
    {
        void Click(System.Windows.Controls.Primitives.ButtonBase button) { button.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent)); Pump(); }
        void Message(object message) => receive(JsonSerializer.Serialize(message));
        receive("""{"event":"stopped"}""");
        window.ShowPage("home");
        int Stops() => window.SelfTestCommands.Count(item => item.Text("command") == "stop");
        var stops = Stops();
        Click(window.CodecSettingsButton);
        var policy = window.PolicyWindow!;
        checks["codec_gear_reuses_home_row_and_opens_policy"] = policy.IsVisible && policy.Title.Contains("连接与编码") && Math.Abs(window.CodecSettingsButton.TranslatePoint(new(0,0), window).Y - window.CodecPicker.TranslatePoint(new(0,0), window).Y) < 3;
        policy.PolicyPicker.SelectedIndex = 1; window.FlushSettings().GetAwaiter().GetResult();
        checks["codec_policy_and_settings_le_switch_stay_synchronized"] = window.LeAudioToggle.IsChecked == false && window.ConnectionPolicyPicker.SelectedIndex == 1 && window.SelfTestCommands.Last().GetProperty("values").Text("connection_policy") == "classic";
        var sbc = policy.CodecChecks.Children.OfType<CheckBox>().Single(item => Equals(item.Tag, "sbc"));
        var ldac = policy.CodecChecks.Children.OfType<CheckBox>().Single(item => Equals(item.Tag, "ldac"));
        checks["sbc_compatibility_is_always_selected_and_cannot_be_unchecked"] = sbc.IsChecked == true && !sbc.IsEnabled;
        ldac.IsChecked = true; window.FlushSettings().GetAwaiter().GetResult();
        checks["allowed_codec_change_is_separate_from_preference"] = window.SelfTestCommands.Last().GetProperty("values").GetProperty("allowed_codecs").EnumerateArray().Any(item => item.GetString() == "ldac");
        ldac.IsChecked = false; window.FlushSettings().GetAwaiter().GetResult();
        checks["disabled_codec_stays_visible_but_dimmed"] = ldac.Opacity < 1 && window.CodecPicker.Items.OfType<ComboBoxItem>().Single(item => Equals(item.Tag, "ldac")).IsEnabled == false;
        policy.PolicyPicker.SelectedIndex = 0; window.FlushSettings().GetAwaiter().GetResult();
        checks["le_preference_saves_without_stopping_receiver"] = Stops() == stops && window.LeAudioToggle.IsChecked == true;
        Capture(policy, Path.Combine(folder, "connection-codec-policy.png")); policy.Close(); Pump();
        window.ShowPage("settings");
        window.ConnectionPolicyPicker.SelectedIndex = 1; Pump(400);
        checks["application_settings_save_automatically_without_save_button"] = window.FindName("SaveButton") == null && window.SettingsHint.Text.Contains("自动保存") && window.SelfTestCommands.Last(item => item.Text("command") == "preferences").GetProperty("values").Text("connection_policy") == "classic";
        window.LeAudioToggle.IsChecked = true; window.FlushSettings().GetAwaiter().GetResult();
        checks["enabling_le_updates_application_policy_picker"] = window.ConnectionPolicyPicker.SelectedIndex == 0;
        window.ShowPage("home");
        receive("""{"event":"starting"}""");
        receive("""{"event":"device","key":"codec-target","state":"已连接"}""");
        receive("""{"event":"device","key":"codec-other","state":"已连接"}""");
        window.OpenDeviceCodec(window.State.Devices.Single(card => card.Key == "codec-target")); Pump();
        var device = window.DeviceCodecFor("codec-target")!;
        var optionsRequest = window.SelfTestCommands.Last(item => item.Text("command") == "codec_options").Number("request_id");
        Message(new { @event = "codec_options", key = "codec-target", request_id = optionsRequest, success = true, protocol = "classic", session_id = "0123456789abcdef01234567",
            actual = new { codec = "aac", rate = 48000 }, selection = new { codec = "auto", rate = 0, depth = 0, bitrate = 0, quality = "auto" },
            codecs = new object[] { new { codec = "aac", name = "AAC", supported = true, rates = new[] { 44100, 48000 }, depths = Array.Empty<int>(), quality_writable = true, bitrate_writable = true },
                new { codec = "lhdc5", name = "LHDC 5.0", supported = true, rates = new[] { 48000, 96000 }, depths = new[] { 16, 24 }, quality_writable = false, bitrate_writable = false, quality_reason = "质量档位与实际发送码率由发送设备决定。" } } });
        device.Codec.SelectedIndex = 2; Pump();
        checks["per_peer_parameters_expose_real_supported_rates_and_readonly_quality"] = device.Rate.Items.Count == 3 && device.DepthRow.IsVisible && device.QualityRow.Visibility == Visibility.Collapsed && device.QualityReason.Text.Contains("发送设备");
        device.Rate.SelectedIndex = 2; device.Depth.SelectedIndex = 2;
        Capture(device, Path.Combine(folder, "device-codec-quality.png"));
        Click(device.ApplyButton);
        var request = window.SelfTestCommands.Last(item => item.Text("command") == "codec_switch");
        checks["codec_switch_is_scoped_to_peer_and_connection_token"] = request.Text("key") == "codec-target" && request.Text("session_id") == "0123456789abcdef01234567" && request.GetProperty("selection").Number("rate") == 96000 && request.GetProperty("selection").Number("depth") == 24;
        checks["codec_switch_progress_disables_duplicate_apply_without_touching_other_cards"] = !device.ApplyButton.IsEnabled && window.State.Devices.Count == 2 && Stops() == stops;
        Message(new { @event = "codec_switch", key = "codec-target", request_id = request.Number("request_id") - 1, success = true, done = true, actual = new { codec = "lhdc5", rate = 192000 }, message = "obsolete" });
        checks["late_codec_response_cannot_complete_a_new_request"] = !device.ApplyButton.IsEnabled && !device.ActualFormat.Text.Contains("192");
        Message(new { @event = "codec_switch", key = "codec-target", request_id = request.Number("request_id"), success = true, done = true, actual = new { codec = "lhdc5", rate = 96000 }, message = "本次连接的编码已调整。" });
        checks["completed_switch_displays_actual_format"] = device.ApplyButton.IsEnabled && device.ActualFormat.Text.Contains("LHDC 5.0 · 96 kHz");
        Message(new { @event = "codec_state", key = "codec-target", actual = new { codec = "aac", rate = 48000 }, reason = "发送设备选择了 AAC，没有采用首选 LDAC。" });
        checks["fallback_reason_is_kept_with_device_details"] = window.State.Devices.Single(card => card.Key == "codec-target").CodecNote.Contains("发送设备选择") && device.ActualFormat.Text.Contains("AAC");
        receive("""{"event":"device","key":"codec-target","state":"已断开"}""");
        checks["disconnect_invalidates_open_codec_dialog"] = !device.ApplyButton.IsEnabled && !device.Refresh.IsEnabled && device.Status.Text.Contains("连接已结束");
        device.Close();
        receive("""{"event":"codec_state","key":"old-connection","actual":{"codec":"ldac","rate":96000}}""");
        checks["codec_events_never_resurrect_old_devices"] = window.State.Devices.All(card => card.Key != "old-connection");
        window.OpenDeviceCodec(window.State.Devices.Single()); Pump();
        device = window.DeviceCodecFor("codec-other")!;
        optionsRequest = window.SelfTestCommands.Last(item => item.Text("command") == "codec_options").Number("request_id");
        Message(new { @event = "codec_options", key = "codec-other", request_id = optionsRequest, success = true, protocol = "le", codecs = Array.Empty<object>(), actual = new { codec = "lc3", rate = 48000 }, reason = "LE Audio / LC3 参数由发送设备协商。" });
        checks["le_options_are_truthfully_readonly_without_classic_controls"] = device.Choices.Visibility == Visibility.Collapsed && !device.ApplyButton.IsEnabled && device.ActualFormat.Text.Contains("LE Audio / LC3");
        Capture(device, Path.Combine(folder, "device-lc3-readonly.png")); device.Close();
        receive("""{"event":"stopped"}""");
    }
}
