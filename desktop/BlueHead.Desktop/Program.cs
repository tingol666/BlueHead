using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;

namespace BlueHead.Desktop;

internal static class Program
{
    [STAThread]
    public static int Main(string[] arguments)
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnMainWindowClose };
        app.Resources.MergedDictionaries.Add((ResourceDictionary)Application.LoadComponent(new Uri("/BlueHead;component/Styles.xaml", UriKind.Relative)));
        Wpf.Ui.Appearance.ApplicationThemeManager.Apply(Wpf.Ui.Appearance.ApplicationTheme.Light, Wpf.Ui.Controls.WindowBackdropType.None);
        Wpf.Ui.Appearance.ApplicationAccentColorManager.Apply(Color.FromRgb(7, 139, 78), Wpf.Ui.Appearance.ApplicationTheme.Light);
        if (arguments is ["--self-test", var folder]) return SelfTest(app, folder);
        if (arguments is ["--engine-smoke", var report]) return EngineSmoke(app, report);
        if (arguments.Contains("--recovery")) { app.Run(new RecoveryWindow()); return 0; }
        using var mutex = new Mutex(true, "Local\\BlueHead.Desktop." + Environment.UserName, out var first);
        if (!first) { MessageBox.Show("BlueHead 已在运行，可从系统托盘打开。", "BlueHead"); return 0; }
        var main = new MainWindow();
        if (arguments.Contains("--diagnostics")) main.Loaded += (_, _) => main.ShowPage("help");
        if (arguments.Contains("--tray")) main.Loaded += (_, _) => main.Hide();
        app.DispatcherUnhandledException += (_, error) =>
        { error.Handled = true; MessageBox.Show(main, error.Exception.Message, "操作未完成", MessageBoxButton.OK, MessageBoxImage.Information); };
        app.Run(main); return 0;
    }
    private static void Capture(FrameworkElement window, string path)
    {
        window.UpdateLayout();
        var image = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
        image.Render(window);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(image));
        using var stream = File.Create(path); encoder.Save(stream);
    }
    private static void Pump(int milliseconds = 80)
    {
        var frame = new DispatcherFrame();
        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(milliseconds) };
        timer.Tick += (_, _) => { timer.Stop(); frame.Continue = false; };
        timer.Start(); Dispatcher.PushFrame(frame);
    }
    private static int SelfTest(Application app, string folder)
    {
        Directory.CreateDirectory(folder);
        var checks = new Dictionary<string, bool>();
        var window = new MainWindow(true); app.MainWindow = window;
        window.Show();
        WindowSizing.Apply(window, true); Pump();
        checks["adaptive_start_fits_actual_monitor_work_area"] = WindowSizing.FitsCurrentWorkArea(window);
        var screens = new[] { (1920d, 1040d, 1d), (1920d, 1040d, 1.25), (2560d, 1560d, 1.5), (3840d, 2120d, 2d), (1280d, 680d, 1.5), (768d, 1310d, 1d) };
        checks["adaptive_geometry_handles_dpi_small_and_portrait_screens"] = screens.All(screen =>
        {
            var work = new Size(screen.Item1 / screen.Item3, screen.Item2 / screen.Item3);
            var size = WindowSizing.Preferred(work);
            return size.Width > 0 && size.Height > 0 && size.Width < work.Width && size.Height < work.Height && size.Width <= 1000 && size.Height <= 720;
        });
        checks["adaptive_size_changes_with_work_area"] = WindowSizing.Preferred(new(1536, 824)) != WindowSizing.Preferred(new(2560, 1400));
        window.Width = window.MinWidth; window.Height = window.MinHeight; Pump();
        WindowSizing.Apply(window, false);
        checks["display_change_preserves_user_smaller_size"] = window.Width == window.MinWidth && window.Height == window.MinHeight;
        WindowSizing.Apply(window, true); Pump();
        window.EngineState.Text = "界面示例 · 无真实连接";
        app.Dispatcher.Invoke(() => { }, DispatcherPriority.ApplicationIdle);
        void Event(string json) { using var document = JsonDocument.Parse(json); window.ApplyEvent(document.RootElement); }
        Event("""{"event":"inventory","adapters":[],"audio":[]}""");
        checks["first_use_exposes_bluetooth_setup_on_home"] = window.SetupCard.IsVisible && window.NeedsBluetoothSetup && Equals(window.StartButton.Content, "设置蓝牙接收") && !window.EmptyState.IsVisible;
        Capture(window, Path.Combine(folder, "first-use.png"));
        Event("""{"event":"inventory","adapters":[{"transport":"usb:8087:0033#0","name":"Intel AX211"}],"audio":[{"index":1,"name":"电脑耳机（界面示例）","max_output_channels":2,"max_input_channels":0},{"index":2,"name":"麦克风（界面示例）","max_output_channels":0,"max_input_channels":1}]}""");
        checks["prepared_adapter_shows_start_and_pairing"] = !window.SetupCard.IsVisible && !window.NeedsBluetoothSetup && window.EmptyState.IsVisible && Equals(window.StartButton.Content, "开始接收");
        checks["labels_distinguish_mobile_devices_and_computer_inputs"] = window.PlaybackGain.Label == "设备声音" && window.MicrophoneGain.Label == "电脑麦克风" && window.SystemGain.Label == "电脑声音";
        checks["codec_and_playback_mode_share_home_row"] = window.CodecPicker.IsVisible && window.PlaybackModePicker.IsVisible && Math.Abs(window.CodecPicker.TranslatePoint(new(0,0),window).Y - window.PlaybackModePicker.TranslatePoint(new(0,0),window).Y) < 2;
        checks["mix_switch_is_on_home_and_off_means_two_controls"] = window.SystemAudioToggle.IsVisible && window.SystemGain.Visibility == Visibility.Collapsed;
        Capture(window, Path.Combine(folder, "home-empty.png"));
        Event("""{"event":"starting"}""");
        Event("""{"event":"status","message":"已就绪：在移动设备蓝牙列表连接 BlueHead PC"}""");
        Event("""{"event":"device","key":"demo-phone","state":"已连接"}""");
        Event("""{"event":"name","key":"demo-phone","name":"手机（界面示例）"}""");
        Event("""{"event":"format","key":"demo-phone","codec":"aac","rate":48000,"channels":2}""");
        Event("""{"event":"device","key":"demo-tablet","state":"已连接"}""");
        Event("""{"event":"name","key":"demo-tablet","name":"平板（界面示例）"}""");
        Event("""{"event":"call","key":"demo-phone","state":"通话中 · 麦克风已启用","phase":"active"}""");
        checks["device_cards_and_actual_codec"] = window.State.Devices.Count == 2 && window.State.Devices[0].MediaDescription.Contains("AAC");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-phone","media":{},"call":{"active":true,"codec":"msbc","rate":16000,"rx_bitrate":60800,"tx_bitrate":60800,"rx_packets":800,"microphone_active":true,"system_audio_active":false}},{"key":"demo-tablet","media":{"codec":"aptx_hd","rate":48000,"bitrate":576000,"packets_per_second":100,"playing":true,"buffer_ms":60,"recoveries":0},"call":{}}]}""");
        checks["multiple_connections_offer_explained_choice"] = window.PlaybackModePicker.IsVisible && window.PlaybackModePicker.Items.Count == 2;
        window.CodecPicker.SelectedItem = window.CodecPicker.Items.OfType<System.Windows.Controls.ComboBoxItem>().First(item => Equals(item.Tag, "aptx_hd"));
        checks["codec_change_is_deferred_while_voice_in_use"] = window.CodecPicker.IsEnabled && window.CodecApplyRow.IsVisible && !window.ApplyCodecButton.IsEnabled && window.CodecApplyHint.Text.Contains("语音通道");
        window.CodecPicker.SelectedIndex = 0;
        window.PlaybackModePicker.SelectedIndex = 1;
        checks["mode_choice_sends_explicit_live_command"] = window.SelfTestCommands.Last().Text("command") == "media_mode" && window.SelfTestCommands.Last().Text("mode") == "single";
        Event("""{"event":"media_mode","success":true,"mode":"single"}""");
        checks["single_mode_exposes_per_device_play_selection"] = window.State.Devices.All(card => card.CanSelectMedia) && window.MediaModeHint.Text.Contains("自动接替");
        window.PlaybackModePicker.SelectedIndex = 0;
        Event("""{"event":"media_mode","success":true,"mode":"mix"}""");
        window.UpdateLayout();
        var deviceContainers = window.DeviceCards.ItemContainerGenerator.ContainerFromIndex(0) as FrameworkElement;
        checks["device_card_vertical_spacing_is_compact"] = deviceContainers is { ActualHeight: <= 62 };
        checks["reception_status_and_start_button_are_vertically_centered"] = Math.Abs(window.ReceptionStatus.TranslatePoint(new(0,window.ReceptionStatus.ActualHeight/2),window).Y - window.StartButton.TranslatePoint(new(0,window.StartButton.ActualHeight/2),window).Y) < 1.5;
        checks["start_and_selectors_are_small_controls"] = window.StartButton.ActualHeight <= 32 && window.CodecPicker.ActualHeight <= 32 && window.PlaybackModePicker.ActualHeight <= 32;
        IEnumerable<T> Visuals<T>(DependencyObject parent) where T : DependencyObject
        {
            for (var i=0; i<System.Windows.Media.VisualTreeHelper.GetChildrenCount(parent); i++)
            {
                var child=System.Windows.Media.VisualTreeHelper.GetChild(parent,i);
                if (child is T match) yield return match;
                foreach(var nested in Visuals<T>(child)) yield return nested;
            }
        }
        var meta=Visuals<System.Windows.Controls.TextBlock>(window.DeviceCards).First(item=>item.Text==window.State.Devices[0].MediaDescription);
        var anchor=Visuals<System.Windows.Controls.Primitives.ToggleButton>(window.DeviceCards).First(item=>item.Name=="DetailsAnchor");
        checks["details_anchor_follows_live_protocol_on_same_line"] = Math.Abs(meta.TranslatePoint(new(0,meta.ActualHeight/2),window).Y-anchor.TranslatePoint(new(0,anchor.ActualHeight/2),window).Y)<2 && anchor.TranslatePoint(new(0,0),window).X>meta.TranslatePoint(new(0,0),window).X;
        var rowHeight=deviceContainers!.ActualHeight;
        window.State.Devices[0].DetailsOpen=true; Pump();
        var details=Visuals<System.Windows.Controls.Primitives.Popup>(window.DeviceCards).First(item=>item.Name=="DeviceDetailsPopup" && Equals(item.Tag,window.State.Devices[0]));
        checks["details_popup_displays_real_text_without_growing_device_row"] = details.IsOpen && details.Child.IsVisible && deviceContainers.ActualHeight==rowHeight;
        Capture((FrameworkElement)details.Child,Path.Combine(folder,"device-details.png"));
        window.PlaybackGain.IsOpen=true; Pump();
        checks["volume_editor_closes_details_popup"] = !window.State.Devices[0].DetailsOpen;
        window.CloseAudioEditors();
        window.PlaybackModePicker.SelectedIndex=1;
        Event("""{"event":"media_mode","success":true,"mode":"single"}""");
        Event("""{"event":"media_owner","key":"demo-tablet"}"""); Pump();
        checks["owner_event_updates_inline_selected_badge"] = window.State.Devices[1].IsMediaSelected && !window.State.Devices[0].IsMediaSelected && window.State.Devices[1].MediaAction=="当前播放";
        var name=Visuals<System.Windows.Controls.TextBlock>(window.DeviceCards).First(item=>item.Text==window.State.Devices[1].Name);
        var badge=Visuals<Wpf.Ui.Controls.Button>(window.DeviceCards).First(item=>Equals(item.DataContext,window.State.Devices[1]) && Equals(item.Content,"当前播放"));
        checks["playback_badge_is_centered_next_to_device_name"] = Math.Abs(name.TranslatePoint(new(0,name.ActualHeight/2),window).Y-badge.TranslatePoint(new(0,badge.ActualHeight/2),window).Y)<1.5;
        checks["playback_badge_immediately_follows_name"] = badge.TranslatePoint(new(0,0),window).X - name.TranslatePoint(new(name.ActualWidth,0),window).X is >= 6 and <= 10;
        var actions = Visuals<System.Windows.Controls.StackPanel>(window.DeviceCards).First(item=>item.Name=="DeviceActions" && Equals(item.DataContext,window.State.Devices[1]));
        var metadata = Visuals<System.Windows.Controls.Grid>(window.DeviceCards).First(item=>item.Name=="DeviceMetadataLine" && Equals(item.DataContext,window.State.Devices[1]));
        var nameLine = Visuals<System.Windows.Controls.Grid>(window.DeviceCards).First(item=>item.Name=="DeviceNameLine" && Equals(item.DataContext,window.State.Devices[1]));
        var leftTop = nameLine.TranslatePoint(new(0,0),window).Y;
        var leftBottom = metadata.TranslatePoint(new(0,metadata.ActualHeight),window).Y;
        checks["device_actions_center_on_both_information_lines"] = Math.Abs(actions.TranslatePoint(new(0,actions.ActualHeight/2),window).Y - (leftTop+leftBottom)/2) < 1.5;
        checks["device_information_lines_have_no_extra_vertical_gap"] = metadata.TranslatePoint(new(0,0),window).Y-nameLine.TranslatePoint(new(0,nameLine.ActualHeight),window).Y is >= -1 and <= 1;
        Capture(window,Path.Combine(folder,"home-single-demo.png"));
        var originalDeviceName = window.State.Devices[1].Name;
        window.Width = window.MinWidth; window.Height = window.MinHeight;
        window.State.Devices[1].Name = "很长的设备名称，用于检查小窗口下名称省略与播放标记、音量操作之间的边界"; Pump();
        checks["long_name_at_minimum_size_keeps_badge_and_actions_separate"] = name.ActualWidth <= name.MaxWidth && badge.TranslatePoint(new(badge.ActualWidth,0),window).X + 8 <= actions.TranslatePoint(new(0,0),window).X;
        checks["long_metadata_at_minimum_size_keeps_details_and_actions_separate"] = metadata.TranslatePoint(new(metadata.ActualWidth,0),window).X + 8 <= actions.TranslatePoint(new(0,0),window).X;
        Capture(window,Path.Combine(folder,"home-long-name-minimum.png"));
        window.State.Devices[1].Name = originalDeviceName;
        WindowSizing.Apply(window,true); Pump();
        window.PlaybackModePicker.SelectedIndex=0;
        Event("""{"event":"media_mode","success":true,"mode":"mix"}"""); Pump();

        Capture(window, Path.Combine(folder, "home-connected-demo.png"));
        window.PlaybackGain.IsOpen = true; Pump();
        checks["volume_editor_opens_with_bound_device_picker"] = window.PlaybackGain.EditorPanel.IsVisible && window.OutputPicker.Items.Count == window.State.Outputs.Count && window.OutputPicker.SelectedItem is AudioChoice;
        Capture(window.PlaybackGain.EditorPanel, Path.Combine(folder, "volume-playback.png"));
        window.MicrophoneGain.IsOpen = true; Pump();
        checks["opening_another_editor_closes_previous"] = !window.PlaybackGain.IsOpen && window.MicrophoneGain.EditorPanel.IsVisible;
        Capture(window.MicrophoneGain.EditorPanel, Path.Combine(folder, "volume-microphone.png"));
        window.ShowPage("settings");
        checks["page_navigation_closes_editors"] = !window.MicrophoneGain.IsOpen;
        Capture(window, Path.Combine(folder, "settings.png"));
        var advancedHeader = window.AdvancedAudio.Template.FindName("HeaderButton", window.AdvancedAudio) as FrameworkElement;
        checks["advanced_settings_has_large_full_width_entry"] = advancedHeader is { ActualHeight: >= 60 } && advancedHeader.ActualWidth > 500;
        window.ShowAdvancedAudio();
        var frame = new DispatcherFrame();
        var renderTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(350) };
        renderTimer.Tick += (_, _) => { renderTimer.Stop(); frame.Continue = false; };
        renderTimer.Start(); Dispatcher.PushFrame(frame);
        checks["advanced_mixer_explanation_secondary"] = window.AdvancedAudio.IsExpanded && window.MediaBufferPicker.IsVisible && !window.SystemAudioToggle.IsVisible;
        Capture(window, Path.Combine(folder, "advanced-mix.png"));
        window.AdvancedAudio.IsExpanded = false;
        window.ShowPage("adapters"); Capture(window, Path.Combine(folder, "adapters.png"));
        Event("""{"event":"device","key":"demo-tablet","state":"已断开"}""");
        checks["disconnected_card_removed"] = window.State.Devices.Count == 1;
        Event("""{"event":"starting"}""");
        checks["live_audio_controls_enabled"] = window.OutputPicker.IsEnabled && window.InputPicker.IsEnabled;
        checks["connection_settings_locked_while_receiving"] = !window.AdapterPicker.IsEnabled && !window.NameEntry.IsEnabled;
        window.OutputPicker.SelectedIndex = 1;
        checks["output_switch_waits_for_ack"] = !window.OutputPicker.IsEnabled;
        Event("""{"event":"audio_device","direction":"output","device":1,"request_id":1,"success":true}""");
        checks["output_switch_ack_keeps_connection"] = window.OutputPicker.IsEnabled && window.State.Devices.Count == 1;
        window.InputPicker.SelectedIndex = 1;
        Event("""{"event":"audio_device","direction":"input","device":2,"request_id":2,"success":false,"message":"测试：麦克风不可用，已保留原设备"}""");
        checks["failed_input_switch_rolls_back"] = window.InputPicker.IsEnabled && window.InputPicker.SelectedIndex == 0 && window.State.Devices.Count == 1;
        Event("""{"event":"call","key":"demo-phone","state":"来电","phase":"incoming"}""");
        checks["incoming_call_actions"] = window.State.Devices[0].CanAnswer && window.State.Devices[0].CanHangup;
        Event("""{"event":"call","key":"demo-phone","state":"通话中","phase":"active"}""");
        checks["active_call_actions"] = !window.State.Devices[0].CanAnswer && window.State.Devices[0].CanHangup;
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-phone","media":{"codec":"aptx_hd","rate":48000,"bitrate":576000,"packets_per_second":100,"playing":true,"buffer_ms":60,"recoveries":2},"call":{}}]}""");
        checks["live_media_protocol_rate_measured_bitrate"] = window.State.Devices[0].MediaDescription.Contains("aptX HD") && window.State.Devices[0].MediaDescription.Contains("576 kb/s") && window.State.Devices[0].LiveDetails.Contains("重同步 2");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-phone","media":{},"call":{"active":true,"codec":"msbc","rate":16000,"rx_bitrate":60800,"tx_bitrate":60800,"rx_packets":160,"microphone_active":true,"system_audio_active":true}}]}""");
        checks["call_uses_same_live_protocol_line"] = window.State.Devices[0].MediaDescription.Contains("HFP / mSBC") && window.State.Devices[0].MediaDescription.Contains("60.8 kb/s") && window.State.Devices[0].LiveDetails.Contains("电脑声音已加入");
        window.PlaybackGain.Value = 180; window.MicrophoneGain.Value = 160;
        window.State.Devices[0].Volume = 175; window.State.Devices[0].UplinkVolume = 130;
        checks["independent_device_and_master_levels_0_to_200"] = window.PlaybackGain.Maximum == 200 && window.MicrophoneGain.Maximum == 200 && window.SystemGain.Maximum == 200 && window.State.Devices[0].Volume == 175 && window.State.Devices[0].UplinkVolume == 130;
        window.SystemAudioToggle.IsChecked = true;
        Event("""{"event":"system_audio","success":true,"enabled":true,"request_id":3}""");
        checks["advanced_mix_can_enable_live"] = window.SystemGain.IsEnabled && window.SystemGain.CanAdjust && window.SystemAudioToggle.IsEnabled;
        window.ShowPage("home"); Pump();
        checks["mix_enabled_shows_third_master"] = window.SystemGain.IsVisible;
        foreach (var master in new[] { window.PlaybackGain, window.MicrophoneGain, window.SystemGain })
        {
            master.Value = 168; master.ToggleSilence();
            checks["mute_zero_" + master.Tag] = master.Value == 0 && master.IsSilent;
            master.ToggleSilence();
            checks["unmute_restores_previous_" + master.Tag] = master.Value == 168 && !master.IsSilent;
        }
        Capture(window, Path.Combine(folder, "home-mixing.png"));
        window.SystemGain.IsOpen = true; Pump();
        Capture(window.SystemGain.EditorPanel, Path.Combine(folder, "volume-system.png"));
        window.CloseAudioEditors();
        var levels = window.AudioEditors().Where(level => level.Compact).ToArray();
        checks["device_editors_are_compact_and_sliders_hidden"] = levels.Length == 2 && levels.All(level => !level.IsOpen && !level.EditorSlider.IsVisible);
        var down = levels.FirstOrDefault(level => level.Icon != "\uE720");
        var up = levels.FirstOrDefault(level => level.Icon == "\uE720");
        if (down != null && up != null)
        {
            down.ToggleSilence(); up.ToggleSilence();
            checks["device_mute_paths_are_independent"] = window.State.Devices[0].Volume == 0 && window.State.Devices[0].UplinkVolume == 0;
            down.ToggleSilence(); up.ToggleSilence();
            checks["device_unmute_restores_both_previous_levels"] = window.State.Devices[0].Volume == 175 && window.State.Devices[0].UplinkVolume == 130;
            down.ToggleSilence(); Pump(160);
            checks["device_mute_sends_zero_to_matching_engine_route"] = window.SelfTestCommands.Any(command => command.Text("command") == "gain" && command.Text("key") == "demo-phone" && command.Text("direction") == "down" && command.Number("gain") == 0);
            down.ToggleSilence(); Pump(160);
            checks["device_unmute_sends_restored_gain"] = window.SelfTestCommands.Last(command => command.Text("command") == "gain" && command.Text("direction") == "down").Number("gain") == 1.75;
            down.IsOpen = true; Pump(); down.EditorSlider.Value = 200; Pump();
            checks["popup_slider_updates_bound_device_only"] = window.State.Devices[0].Volume == 200 && window.State.Devices[0].UplinkVolume == 130;
            down.ResetLevel(); Pump();
            checks["reset_keeps_two_way_binding"] = window.State.Devices[0].Volume == 100;
            window.State.Devices[0].Volume = 175; Pump();
            checks["external_volume_updates_existing_popup"] = down.Value == 175 && down.EditorSlider.Value == 175;
            Capture(down.EditorPanel, Path.Combine(folder, "volume-device.png"));
            down.EditorPanel.RaiseEvent(new System.Windows.Input.KeyEventArgs(System.Windows.Input.Keyboard.PrimaryDevice, System.Windows.PresentationSource.FromVisual(down.EditorPanel), 0, System.Windows.Input.Key.Escape) { RoutedEvent = System.Windows.Input.Keyboard.PreviewKeyDownEvent });
            checks["escape_closes_volume_editor"] = !down.IsOpen;
        }
        window.Notice.Visibility = Visibility.Collapsed;
        Event("""{"event":"status","message":"已就绪：在移动设备蓝牙列表连接 BlueHead PC"}""");
        window.PlaybackGain.Value = 145; window.PlaybackGain.ToggleSilence(); Pump(160);
        checks["master_mute_sends_zero_to_engine"] = window.SelfTestCommands.Any(command => command.Text("command") == "master_gain" && command.Text("bus") == "playback" && command.Number("gain") == 0);
        window.PlaybackGain.ToggleSilence(); Pump(160);
        checks["master_unmute_sends_restored_gain"] = window.SelfTestCommands.Last(command => command.Text("command") == "master_gain" && command.Text("bus") == "playback").Number("gain") == 1.45;
        window.Width = window.MinWidth; window.Height = window.MinHeight; Pump();
        checks["minimum_window_keeps_all_master_controls_inside"] = window.MasterControls.Children.OfType<AudioLevel>().Where(level => level.IsVisible).All(level => level.ActualWidth > 150 && level.TranslatePoint(new(0, 0), window).X + level.ActualWidth <= window.ActualWidth);
        Capture(window, Path.Combine(folder, "home-minimum.png"));
        window.Width = 940; window.Height = 580; Pump();
        window.MuteToggle.IsChecked = true;
        checks["uplink_mute_is_explicit"] = window.TransmitHint.Text.Contains("麦克风与电脑声音均不发送") && window.MutedIndicator.IsVisible && window.MicrophoneGain.Secondary.Contains("已静音");
        window.MicrophoneGain.IsOpen = true; Pump(); Capture(window.MicrophoneGain.EditorPanel, Path.Combine(folder, "volume-muted.png")); window.CloseAudioEditors();
        window.MuteToggle.IsChecked = false;

        Event("""{"event":"audio_refreshing"}""");
        checks["device_refresh_blocks_stale_selections"] = !window.OutputPicker.IsEnabled && !window.InputPicker.IsEnabled;
        Event("""{"event":"audio_refreshed","success":true,"output_device":null,"input_device":null}""");
        checks["device_refresh_preserves_bluetooth_cards"] = window.OutputPicker.IsEnabled && window.State.Devices.Count == 1;
        Event("""{"event":"stopped"}""");
        checks["stop_clears_connections"] = window.State.Devices.Count == 0 && window.AdapterPicker.IsEnabled;
        Event("""{"event":"call","key":"demo-phone","state":"通话服务已断开，可重连"}""");
        Event("""{"event":"microphone","key":"demo-phone","active":false}""");
        checks["late_events_do_not_resurrect_disconnected_devices"] = window.State.Devices.Count == 0;
        window.ShowPage("home");
        Event("""{"event":"starting"}""");
        window.CodecPicker.SelectedItem = window.CodecPicker.Items.OfType<System.Windows.Controls.ComboBoxItem>().First(item => Equals(item.Tag, "aac_192"));
        Event("""{"event":"audio_refreshing"}""");
        var beforeRefreshApply = window.SelfTestCommands.Count;
        window.ApplyCodecProfile().GetAwaiter().GetResult();
        checks["audio_device_refresh_blocks_codec_restart"] = !window.ApplyCodecButton.IsEnabled && window.SelfTestCommands.Count == beforeRefreshApply && window.CodecApplyHint.Text.Contains("更新");
        Event("""{"event":"audio_refreshed","success":true,"output_device":null,"input_device":null}""");
        checks["reconnect_button_explains_connection_interruption"] = window.CodecApplyRow.IsVisible && window.CodecApplyHint.Text.Contains("断开") && window.ApplyCodecButton.IsEnabled;
        window.ApplyCodecProfile().GetAwaiter().GetResult();
        checks["applying_codec_stops_before_starting"] = window.SelfTestCommands.Last().Text("command") == "stop";
        Event("""{"event":"stopped"}""");
        var restart = window.SelfTestCommands.Last();
        checks["codec_restarts_with_gains_and_mode_preserved"] = restart.Text("command") == "start" && restart.GetProperty("settings").Text("codec") == "aac_192" && restart.GetProperty("settings").Text("media_mode") == "mix" && restart.GetProperty("settings").Number("playback_gain") == 1.45;
        Event("""{"event":"starting"}""");
        Event("""{"event":"device","key":"demo-a","state":"已连接"}""");
        Event("""{"event":"device","key":"demo-b","state":"已连接"}""");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-a","media":{"codec":"aac","rate":48000,"bitrate":180000,"protected":true,"lost_packets":123},"call":{}}]}""");
        checks["protection_is_visible_and_lowest_profile_does_not_offer_same_retry"] = window.State.Devices[0].PlaybackNotice.Contains("隔离") && window.MultipointText.Text.Contains("丢包") && !window.LowerBitrateButton.IsVisible;
        Capture(window, Path.Combine(folder, "home-protection-demo.png"));
        Event("""{"event":"stopped"}""");


        window.ShowPage("help"); Capture(window, Path.Combine(folder, "help.png"));
        checks["brand_and_help_expose_same_requested_github_repository"] = window.BrandProjectLink.NavigateUri.AbsoluteUri == "https://github.com/tingol666/BlueHead" && window.HelpProjectLink.NavigateUri == window.BrandProjectLink.NavigateUri;
        checks["brand_project_link_fits_compact_navigation"] = window.BrandProjectLink.Parent is System.Windows.Controls.TextBlock brand && brand.ActualWidth + brand.TranslatePoint(new(0,0),window).X <= 145;
        checks["advanced_settings_secondary"] = window.HomePage.Visibility != Visibility.Visible;
        Event("""{"event":"inventory","adapters":[],"audio":[]}""");
        window.ShowPage("home");
        checks["removed_adapter_returns_to_setup"] = window.SetupCard.IsVisible && window.NeedsBluetoothSetup;
        window.ModePicker.SelectedIndex = 1;
        checks["shared_mode_does_not_require_dedicated_driver"] = !window.SetupCard.IsVisible && !window.NeedsBluetoothSetup;
        checks["windows_managed_settings_disabled_in_shared_mode"] = !window.NameEntry.IsEnabled && !window.CallsToggle.IsEnabled && !window.CodecPicker.IsEnabled;
        window.DriverPicker.ItemsSource = new[] { new BlueHead.Platform.DriverDevice("UI-DEMO-ONLY", "示例蓝牙适配器", "BthUsb", "", "", true, false) };
        window.DriverPicker.SelectedIndex = 0;
        checks["restore_requires_existing_backup"] = !window.RestoreDriverButton.IsEnabled;
        window.DriverPicker.ItemsSource = new[] { new BlueHead.Platform.DriverDevice("UI-DEMO-ONLY", "示例蓝牙适配器", "WinUSB", "", "", true, true) };
        window.DriverPicker.SelectedIndex = 0;
        window.ShowPage("adapters"); Pump();
        checks["driver_restore_remains_available_in_shared_mode"] = window.RestoreDriverButton.IsVisible && window.RestoreDriverButton.IsEnabled && !window.InstallDriverButton.IsVisible;
        Capture(window, Path.Combine(folder, "shared-mode.png"));
        var passed = checks.Values.All(value => value);
        File.WriteAllText(Path.Combine(folder, "desktop-self-test.json"), JsonSerializer.Serialize(new { passed, checks, scope = "WPF UI state and rendering; no radio links or driver changes" }, new JsonSerializerOptions { WriteIndented = true }));
        window.Close(); return passed ? 0 : 1;
    }
    private static int EngineSmoke(Application app, string report)
    {
        var passed = false;
        app.ShutdownMode = ShutdownMode.OnExplicitShutdown;
        app.Dispatcher.BeginInvoke(async () =>
        {
            using var client = new EngineClient();
            var hello = new TaskCompletionSource(); var inventory = new TaskCompletionSource<int>();
            client.Message += message =>
            {
                if (message.Text("event") == "hello") hello.TrySetResult();
                if (message.Text("event") == "inventory") inventory.TrySetResult(message.GetProperty("audio").GetArrayLength());
            };
            try
            {
                await client.StartAsync(Environment.GetEnvironmentVariable("BLUEHEAD_DATA_DIR") ?? Path.Combine(Path.GetDirectoryName(report)!, "engine-smoke-data"));
                await hello.Task.WaitAsync(TimeSpan.FromSeconds(20));
                await client.SendAsync(new { command = "inventory" });
                var endpoints = await inventory.Task.WaitAsync(TimeSpan.FromSeconds(20));
                await client.ShutdownAsync(); passed = true;
                File.WriteAllText(report, JsonSerializer.Serialize(new { passed, audio_endpoints = endpoints, protocol = 1, clean_shutdown = true, scope = "Packaged desktop-engine handshake and readonly inventory" }));
            }
            catch (Exception error) { File.WriteAllText(report, JsonSerializer.Serialize(new { passed = false, error = error.ToString() })); }
            finally { app.Shutdown(); }
        });
        app.Run(); return passed ? 0 : 1;
    }
}
