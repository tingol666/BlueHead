using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void CodecChecks(MainWindow window, Dictionary<string, bool> checks, string folder, Action<string> receive)
    {
        CodecBitrateChecks(checks);
        checks["home_codec_order_matches_connection_codec_settings"] = window.CodecPicker.Items.OfType<ComboBoxItem>().Select(item => item.Tag?.ToString()).SequenceEqual(new[] { "auto" }.Concat(CodecPolicyWindow.Codecs.Select(item => item.Code)));
        var fresh = new EngineSettings();
        checks["fresh_install_allows_every_available_classic_codec"] = fresh.ldac_enabled && fresh.lhdc5_enabled && CodecPolicyWindow.Allowed(fresh).SetEquals(CodecPolicyWindow.Codecs.Select(item => item.Code));
        var disabled = JsonSerializer.Deserialize<EngineSettings>("{\"ldac_enabled\":false,\"lhdc5_enabled\":false}")!;
        checks["saved_disabled_hd_codecs_are_not_enabled_by_new_defaults"] = !disabled.ldac_enabled && !disabled.lhdc5_enabled && !CodecPolicyWindow.Allowed(disabled).Overlaps(new[] { "ldac", "lhdc5" });
        var legacy = JsonSerializer.Deserialize<EngineSettings>("{\"allowed_codecs\":null,\"ldac_enabled\":true,\"lhdc5_enabled\":true}")!;
        checks["legacy_allowlist_remains_explicitly_distinct_from_fresh_defaults"] = CodecPolicyWindow.Allowed(legacy).Count == 6;
        int CaptureControl(FrameworkElement element, string name)
        {
            element.UpdateLayout();
            var width = (int)Math.Ceiling(element.ActualWidth);
            var height = (int)Math.Ceiling(element.ActualHeight);
            var visual = new DrawingVisual();
            using (var drawing = visual.RenderOpen()) drawing.DrawRectangle(new VisualBrush(element), null, new Rect(0, 0, width, height));
            var bitmap = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(visual);
            var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using (var output = File.Create(Path.Combine(folder, name))) encoder.Save(output);
            var pixels = new byte[width * height * 4]; bitmap.CopyPixels(pixels, width * 4, 0);
            return Enumerable.Range(0, width * height).Count(index => pixels[index * 4 + 3] != 0);
        }
        void Click(System.Windows.Controls.Primitives.ButtonBase button) { button.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent)); Pump(); }
        void Message(object message)
        {
            var encoded = JsonSerializer.SerializeToElement(message);
            EngineClient.ValidateEnvelope(encoded);
            receive(encoded.GetRawText());
        }
        foreach (var malformed in new[] { "null", "[]", "{}", "{\"protocol\":\"classic\"}", "{\"protocol\":null}", "{\"protocol\":[]}", "{\"protocol\":1e99}", "{\"protocol\":1.5}" })
        {
            using var document = JsonDocument.Parse(malformed);
            try { EngineClient.ValidateEnvelope(document.RootElement); checks["codec_wire_rejects_invalid_envelope_" + malformed] = false; }
            catch (IOException) { checks["codec_wire_rejects_invalid_envelope_" + malformed] = true; }
        }
        receive("""{"event":"stopped"}""");
        window.ShowPage("home");
        var gear = (Viewbox)window.CodecSettingsButton.Content;
        var outline = (System.Windows.Shapes.Path)gear.Child;
        checks["codec_settings_icon_is_visible_vector_without_font_dependency"] = outline.Data.Bounds.Width > 18 && outline.Data.Bounds.Height > 18 && outline.Fill is System.Windows.Media.SolidColorBrush && gear.ActualWidth == 16 && gear.ActualHeight == 16 && !window.CodecSettingsButton.FontFamily.Source.Contains("Icons");
        checks["codec_settings_vector_produces_visible_pixels"] = CaptureControl(window.CodecSettingsButton, "codec-settings-vector.png") > 30;
        var tooltip = (ToolTip)window.CodecSettingsButton.ToolTip;
        tooltip.PlacementTarget = window.CodecSettingsButton;
        tooltip.IsOpen = true; Pump();
        var tooltipText = (TextBlock)tooltip.Content;
        checks["codec_settings_chinese_tooltip_uses_text_font_and_fits"] = tooltipText.Text == "连接与编码：LE Audio 偏好、允许的编码与首选编码" && tooltip.FontFamily.Source.Contains("Microsoft YaHei UI") && tooltipText.FontFamily.Source.Contains("Microsoft YaHei UI") && tooltipText.ActualWidth > 150 && tooltipText.ActualHeight >= 13;
        CaptureControl(tooltip, "codec-settings-tooltip.png");
        tooltip.IsOpen = false; Pump();
        checks["codec_reference_labels_fit_compact_home_picker"] = window.CodecPicker.Items.OfType<ComboBoxItem>().All(item =>
        {
            var label = new TextBlock { Text = item.Content?.ToString(), FontFamily = window.CodecPicker.FontFamily, FontSize = window.CodecPicker.FontSize, TextWrapping = TextWrapping.NoWrap };
            label.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
            return label.DesiredSize.Width <= window.CodecPicker.ActualWidth - 35;
        });
        window.CodecPicker.IsDropDownOpen = true; Pump();
        var codecList = (System.Windows.Controls.Primitives.Popup)window.CodecPicker.Template.FindName("PART_Popup", window.CodecPicker);
        CaptureControl((FrameworkElement)codecList.Child, "codec-choice-list.png");
        window.CodecPicker.IsDropDownOpen = false; Pump();
        int Stops() => window.SelfTestCommands.Count(item => item.Text("command") == "stop");
        var stops = Stops();
        Click(window.CodecSettingsButton);
        var policy = window.PolicyWindow!;
        checks["codec_gear_reuses_home_row_and_opens_policy"] = policy.IsVisible && policy.Title.Contains("连接与编码") && Math.Abs(window.CodecSettingsButton.TranslatePoint(new(0,0), window).Y - window.CodecPicker.TranslatePoint(new(0,0), window).Y) < 3;
        policy.PolicyPicker.SelectedIndex = 1; window.FlushSettings().GetAwaiter().GetResult();
        checks["codec_policy_and_settings_le_switch_stay_synchronized"] = window.LeAudioToggle.IsChecked == false && window.ConnectionPolicyPicker.SelectedIndex == 1 && window.SelfTestCommands.Last().GetProperty("values").Text("connection_policy") == "classic";
        var sbc = policy.CheckFor("sbc");
        var ldac = policy.CheckFor("ldac");
        checks["sbc_compatibility_is_always_selected_and_cannot_be_unchecked"] = sbc.IsChecked == true && !sbc.IsEnabled;
        ldac.IsChecked = true; window.FlushSettings().GetAwaiter().GetResult();
        checks["allowed_codec_change_is_separate_from_preference"] = window.SelfTestCommands.Last().GetProperty("values").GetProperty("allowed_codecs").EnumerateArray().Any(item => item.GetString() == "ldac");
        ldac.IsChecked = false; window.FlushSettings().GetAwaiter().GetResult();
        checks["disabled_codec_stays_visible_but_dimmed"] = ldac.Opacity < 1 && window.CodecPicker.Items.OfType<ComboBoxItem>().Single(item => Equals(item.Tag, "ldac")).IsEnabled == false;
        policy.PolicyPicker.SelectedIndex = 0; window.FlushSettings().GetAwaiter().GetResult();
        checks["le_preference_saves_without_stopping_receiver"] = Stops() == stops && window.LeAudioToggle.IsChecked == true;
        checks["every_codec_has_a_vector_settings_button"] = CodecPolicyWindow.Codecs.All(item => policy.GearFor(item.Code).Content is Viewbox view && view.Child is System.Windows.Shapes.Path path && path.Data.Bounds.Width > 18 && !policy.GearFor(item.Code).FontFamily.Source.Contains("Icons"));
        void Choose(ComboBox picker, string tag) => picker.SelectedItem = picker.Items.OfType<ComboBoxItem>().Single(item => Equals(item.Tag, tag));
        foreach (var (code, _) in CodecPolicyWindow.Codecs)
        {
            Click(policy.GearFor(code));
            var editor = policy.ParametersWindow!;
            checks["global_codec_editor_exposes_only_real_controls_" + code] = editor.Rate.Items.Count == (code is "ldac" or "lhdc5" ? 5 : 3) && editor.DepthRow.IsVisible == (code == "lhdc5") && editor.BitrateRow.IsVisible == (code == "aac") && editor.QualityRow.IsVisible == (code is "aac" or "sbc");
            if (code is "ldac" or "lhdc5") checks["private_source_quality_is_not_fabricated_" + code] = editor.QualityReason.Text.Contains("发送设备决定");
            editor.Close(); Pump();
        }
        receive("""{"event":"starting"}""");
        receive("""{"event":"device","key":"codec-peer","state":"已连接"}""");
        receive("""{"event":"name","key":"codec-peer","name":"示例设备"}""");
        void Media(string codec, int depth, string mode = "stereo") => Message(new { protocol = 1, @event = "telemetry", running = true, audio = new { }, devices = new[] { new { key = "codec-peer", media = new { codec, rate = codec is "ldac" or "lhdc5" ? 96000 : 48000, bitrate = 500000, playing = true, depth, depth_status = depth > 0 ? "negotiated" : "not_signalled", channels = 2, channel_mode = mode }, call = new { active = false } } } });
        Media("ldac", 0); Pump();
        policy.OpenParameters("lhdc5"); Pump();
        var lhdc = policy.ParametersWindow!; Choose(lhdc.Rate, "96000"); Choose(lhdc.Depth, "24");
        Capture(lhdc, Path.Combine(folder, "global-lhdc-parameters.png"));
        Click(lhdc.SaveButton); window.FlushSettings().GetAwaiter().GetResult();
        var saved = window.SelfTestCommands.Last(item => item.Text("command") == "preferences");
        var parameters = saved.GetProperty("values").GetProperty("codec_parameters").GetProperty("lhdc5");
        checks["codec_parameters_are_saved_globally_without_device_switch_commands"] = parameters.Number("rate") == 96000 && parameters.Number("depth") == 24 && window.SelfTestCommands.All(item => item.Text("command") is not ("codec_switch" or "codec_options"));
        Message(new { protocol = 1, @event = "preferences", request_id = saved.Number("request_id"), success = true, reconnect_required = true, restart_required = false, message = "已保存，请断开并重新连接设备。" });
        checks["saved_parameters_show_visible_reconnect_steps_and_keep_actual_format"] = window.CodecReconnectNotice.IsVisible && window.CodecReconnectText.Text.Contains("蓝牙设置中断开 BlueHead") && window.State.Devices.Single().MediaDescription.Contains("LDAC") && policy.SaveState.Text.Contains("当前连接仍用原格式");
        checks["codec_row_summarizes_saved_parameters"] = policy.SummaryFor("lhdc5").Contains("96 kHz") && policy.SummaryFor("lhdc5").Contains("24 bit");
        policy.OpenParameters("aac"); Pump();
        var aac = policy.ParametersWindow!; Choose(aac.Bitrate, "192000"); Choose(aac.Quality, "balanced");
        Capture(aac, Path.Combine(folder, "global-aac-parameters.png"));
        Click(aac.SaveButton); window.FlushSettings().GetAwaiter().GetResult();
        saved = window.SelfTestCommands.Last(item => item.Text("command") == "preferences");
        parameters = saved.GetProperty("values").GetProperty("codec_parameters");
        checks["separate_codec_settings_do_not_overwrite_each_other"] = parameters.GetProperty("aac").Number("bitrate") == 192000 && parameters.GetProperty("aac").Text("quality") == "balanced" && parameters.GetProperty("lhdc5").Number("rate") == 96000;
        var aacChoice = window.CodecPicker.Items.OfType<ComboBoxItem>().Single(item => Equals(item.Tag, "aac"));
        checks["home_reference_bitrate_updates_after_saving_codec_parameters"] = Equals(aacChoice.Content, "AAC · 约192 kb/s") && ((TextBlock)((ToolTip)aacChoice.ToolTip).Content).Text.Contains("不是当前连接的实测值");
        checks["aac_has_one_preference_entry_with_independent_bitrate_settings"] = window.CodecPicker.Items.OfType<ComboBoxItem>().Count(item => CodecPolicyWindow.Canonical(item.Tag?.ToString() ?? "") == "aac") == 1;
        Message(new { protocol = 1, @event = "preferences", request_id = saved.Number("request_id"), success = true, reconnect_required = true, restart_required = false, message = "已保存，请重新连接。" });
        Capture(policy, Path.Combine(folder, "connection-codec-policy.png"));
        checks["compact_policy_fits_monitor"] = policy.ActualHeight <= SystemParameters.WorkArea.Height - 48 && policy.ActualWidth == 450;
        policy.Close(); Pump();
        IEnumerable<T> Descendants<T>(DependencyObject parent) where T : DependencyObject
        {
            for (var index = 0; index < VisualTreeHelper.GetChildrenCount(parent); ++index)
            {
                var child = VisualTreeHelper.GetChild(parent, index);
                if (child is T match) yield return match;
                foreach (var item in Descendants<T>(child)) yield return item;
            }
        }
        var menus = Descendants<Wpf.Ui.Controls.Button>(window.DeviceCards).Where(item => item.ContextMenu != null).ToArray();
        checks["device_menu_has_no_codec_hot_switch_entry"] = menus.Length > 0 && menus.All(item => item.ContextMenu.Items.OfType<MenuItem>().All(entry => Equals(entry.Header, "断开连接"))) && typeof(MainWindow).Assembly.GetType("BlueHead.Desktop.DeviceCodecWindow") == null;
        Media("lhdc5", 24);
        checks["details_show_negotiated_depth_and_stereo_mode"] = window.State.Devices.Single().LiveDetails.Contains("样本位数：24 bit") && window.State.Devices.Single().LiveDetails.Contains("声道模式：立体声（2 声道）");
        window.State.Devices.Single().DetailsOpen = true; Pump();
        var details = Descendants<System.Windows.Controls.Primitives.Popup>(window.DeviceCards).Single(item => item.Name == "DeviceDetailsPopup");
        CaptureControl((FrameworkElement)details.Child, "details-sample-depth.png");
        window.State.Devices.Single().DetailsOpen = false;
        Media("sbc", 0, "joint_stereo");
        checks["details_clear_old_depth_and_show_actual_joint_stereo"] = window.State.Devices.Single().LiveDetails.Contains("样本位数：未单独协商") && window.State.Devices.Single().LiveDetails.Contains("联合立体声") && !window.State.Devices.Single().LiveDetails.Contains("24 bit");
        Message(new { protocol = 1, @event = "telemetry", running = true, audio = new { }, devices = new[] { new { key = "codec-peer", media = new { codec = "sbc", rate = 48000 }, call = new { active = true, codec = "msbc", rate = 16000, channels = 1, channel_mode = "mono", depth = 0, depth_status = "not_signalled" } } } });
        checks["voice_details_distinguish_mono_and_unknown_source_depth"] = window.State.Devices.Single().LiveDetails.Contains("单声道（1 声道）") && window.State.Devices.Single().LiveDetails.Contains("样本位数：未单独协商");
        Capture(window, Path.Combine(folder, "home-codec-reconnect.png"));
        receive("""{"event":"device","key":"codec-peer","state":"已断开"}""");
        checks["reconnect_reminder_clears_when_affected_connection_ends"] = window.CodecReconnectNotice.Visibility == Visibility.Collapsed;
        receive("""{"event":"device","key":"codec-race","state":"已连接"}""");
        window.ApplyCodecProfile("aac").GetAwaiter().GetResult();
        var firstReply = window.SelfTestCommands.Last(item => item.Text("command") == "preferences").Number("request_id");
        window.TrayToggle.IsChecked = !window.TrayToggle.IsChecked; window.FlushSettings().GetAwaiter().GetResult();
        var secondReply = window.SelfTestCommands.Last(item => item.Text("command") == "preferences").Number("request_id");
        Message(new { protocol = 1, @event = "preferences", request_id = secondReply, success = true, reconnect_required = false, restart_required = false, message = "最新设置已保存" });
        Message(new { protocol = 1, @event = "preferences", request_id = firstReply, success = true, reconnect_required = true, restart_required = false, message = "较早设置已保存" });
        checks["older_success_keeps_reconnect_requirement_without_overwriting_latest_save_status"] = window.CodecReconnectNotice.IsVisible && window.SettingsHint.Text == "最新设置已保存";
        window.OpenCodecPolicy(); Pump(); policy = window.PolicyWindow!;
        Choose(window.CodecPicker, "aptx"); window.FlushSettings().GetAwaiter().GetResult();
        checks["open_policy_tracks_home_preference_changes"] = Equals(((ComboBoxItem)policy.PreferencePicker.SelectedItem).Tag, "aptx");
        policy.OpenParameters("aac"); Pump(); aac = policy.ParametersWindow!; Choose(aac.Bitrate, "128000"); Click(aac.SaveButton); window.FlushSettings().GetAwaiter().GetResult();
        saved = window.SelfTestCommands.Last(item => item.Text("command") == "preferences");
        checks["gear_save_cannot_restore_stale_home_preference"] = saved.GetProperty("values").Text("codec") == "aptx" && saved.GetProperty("values").GetProperty("codec_parameters").GetProperty("aac").Number("bitrate") == 128000;
        policy.Close(); Pump();
        receive("""{"event":"stopped"}"""); receive("""{"event":"starting"}""");
        receive("""{"event":"device","key":"codec-race","state":"已连接"}""");
        Message(new { protocol = 1, @event = "preferences", request_id = saved.Number("request_id"), success = true, reconnect_required = true, restart_required = true, message = "旧接收会话的回执" });
        checks["late_save_reply_cannot_mark_fresh_receiver_session_pending"] = window.CodecReconnectNotice.Visibility == Visibility.Collapsed;
        window.ShowPage("settings");
        window.ConnectionPolicyPicker.SelectedIndex = 1; window.FlushSettings().GetAwaiter().GetResult();
        saved = window.SelfTestCommands.Last(item => item.Text("command") == "preferences");
        Message(new { protocol = 1, @event = "preferences", request_id = saved.Number("request_id"), success = true, reconnect_required = false, restart_required = true, message = "已保存，请停止并重新开始接收。" });
        window.ShowPage("home");
        checks["le_policy_change_explains_stop_start_then_reconnect"] = window.CodecReconnectNotice.IsVisible && window.CodecReconnectText.Text.Contains("停止接收") && window.CodecReconnectText.Text.Contains("开始接收");
        checks["application_settings_save_automatically_without_save_button"] = window.FindName("SaveButton") == null && window.SelfTestCommands.Last(item => item.Text("command") == "preferences").GetProperty("values").Text("connection_policy") == "classic";
        window.LeAudioToggle.IsChecked = true; window.FlushSettings().GetAwaiter().GetResult();
        checks["enabling_le_updates_application_policy_picker"] = window.ConnectionPolicyPicker.SelectedIndex == 0;
        checks["reverting_to_active_connection_policy_clears_unneeded_restart_prompt"] = window.CodecReconnectNotice.Visibility == Visibility.Collapsed;
        checks["codec_settings_never_disconnect_or_restart_active_connections"] = Stops() == stops && window.SelfTestCommands.All(item => item.Text("command") != "device_action" || item.Text("action") != "disconnect");
        receive("""{"event":"stopped"}""");
        checks["receiver_stop_clears_pending_connection_reminders"] = window.CodecReconnectNotice.Visibility == Visibility.Collapsed;
    }
}
