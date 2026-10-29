using BlueHead.Platform;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;

namespace BlueHead.Desktop;

internal static partial class Program
{
    [STAThread]
    public static int Main(string[] arguments)
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnMainWindowClose };
        app.Resources.MergedDictionaries.Add((ResourceDictionary)Application.LoadComponent(new Uri("/BlueHead;component/Styles.xaml", UriKind.Relative)));
        Wpf.Ui.Appearance.ApplicationThemeManager.Apply(Wpf.Ui.Appearance.ApplicationTheme.Light, Wpf.Ui.Controls.WindowBackdropType.None);
        Wpf.Ui.Appearance.ApplicationAccentColorManager.Apply(Color.FromRgb(7, 139, 78), Wpf.Ui.Appearance.ApplicationTheme.Light);
        if (arguments.Length == 2 && arguments[0] == "--self-test") return SelfTest(app, arguments[1]);
        if (arguments.Length == 2 && arguments[0] == "--engine-smoke") return EngineSmoke(app, arguments[1]);
        if (arguments.Length == 2 && arguments[0] == "--exit-self-test") return ExitSelfTest(app, arguments[1]);
        if (arguments.Contains("--recovery")) { app.Run(new RecoveryWindow()); return 0; }
        using var mutex = new Mutex(true, "Local\\BlueHead.Desktop." + Environment.UserName, out var first);
        if (!first)
        {
            if (!InstanceActivation.TrySignal(InstanceActivation.Name)) MessageBox.Show("BlueHead 正在启动或退出，请稍候再打开。", "BlueHead");
            return 0;
        }
        MainWindow? main = null;
        using var activation = new InstanceActivation(InstanceActivation.Name, () =>
        { if (!app.Dispatcher.HasShutdownStarted) app.Dispatcher.BeginInvoke(() => main?.ShowFromTray()); });
        main = new MainWindow();
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
    private static void Pump(int milliseconds = 80, [System.Runtime.CompilerServices.CallerLineNumber] int line = 0)
    {
        var frame = new DispatcherFrame();
        var dispatcher = Dispatcher.CurrentDispatcher;
        var trace = Environment.GetEnvironmentVariable("BLUEHEAD_UI_TRACE") == "1";
        if (trace) Console.WriteLine($"UI wait {line}: start ({milliseconds} ms)");
        // Keep the diagnostic clock independent of dispatcher timer delivery.
        // Posting an operation also wakes an otherwise quiet virtual desktop.
        // Drain ready input/binding/background work before asserting UI state.
        using (var timer = new System.Threading.Timer(_ =>
            dispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(() => frame.Continue = false)),
            null, milliseconds, Timeout.Infinite))
            Dispatcher.PushFrame(frame);
        if (trace) Console.WriteLine($"UI wait {line}: complete");
    }
    private static int SelfTest(Application app, string folder)
    {
        // Match Application.Run: async UI handlers must resume on the dispatcher.
        System.Threading.SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(app.Dispatcher));
        Directory.CreateDirectory(folder);
        var checks = new Dictionary<string, bool>();
        var window = new MainWindow(true); app.MainWindow = window;
        bool WaitForCommand(int start, Func<JsonElement, bool> expected)
        {
            // Virtual displays may deliver a WPF timer after its nominal due
            // time. Observe this operation's command instead of sleeping for
            // only 40 ms longer than the production debounce interval.
            var elapsed = System.Diagnostics.Stopwatch.StartNew();
            do
            {
                Pump(40);
                if (window.SelfTestCommands.Skip(start).Any(expected)) return true;
            } while (elapsed.ElapsedMilliseconds < 2000);
            return false;
        }
        checks["sidebar_and_help_use_build_version"] = window.SidebarVersion.Text == AppVersion.Label && window.HelpVersion.Text == AppVersion.Title;
        checks["diagnostics_use_build_version"] = JsonSerializer.SerializeToElement(window.CreateDiagnosticsReport()).Text("version") == AppVersion.Number;
        using (var correct = JsonDocument.Parse(JsonSerializer.Serialize(new { version = AppVersion.Number }))) EngineClient.ValidateVersion(correct.RootElement);
        checks["matching_engine_version_is_accepted"] = true;
        try { using var wrong = JsonDocument.Parse("{\"version\":\"0.0.0\"}"); EngineClient.ValidateVersion(wrong.RootElement); checks["mismatched_engine_version_is_rejected"] = false; }
        catch (IOException) { checks["mismatched_engine_version_is_rejected"] = true; }
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
        void Click(System.Windows.Controls.Primitives.ButtonBase button) { button.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent)); Pump(); }
        window.ApplyDriverInventory([]);
        Event("""{"event":"inventory","adapters":[],"audio":[]}""");
        checks["first_use_exposes_bluetooth_setup_on_home"] = window.SetupCard.IsVisible && window.NeedsBluetoothSetup && Equals(window.StartButton.Content, "打开蓝牙驱动设置") && !window.EmptyState.IsVisible;
        Capture(window, Path.Combine(folder, "first-use.png"));
        Event("""{"event":"inventory","adapters":[{"transport":"usb:1234:abcd#0","name":"USB Bluetooth（示例）"}],"audio":[{"index":1,"name":"电脑耳机（界面示例）","max_output_channels":2,"max_input_channels":0},{"index":2,"name":"麦克风（界面示例）","max_output_channels":0,"max_input_channels":1}]}""");
        checks["prepared_adapter_shows_start_and_pairing"] = !window.SetupCard.IsVisible && !window.NeedsBluetoothSetup && window.EmptyState.IsVisible && Equals(window.StartButton.Content, "开始接收");
        var restartAdapter = new DriverDevice(@"USB\VID_1234&PID_ABCD\offline", "USB Bluetooth（示例）", "WinUSB", "offline.inf", "", false, false) { RebootRequired = true };
        window.ApplyDriverInventory([restartAdapter]); Pump();
        checks["pending_restart_is_visible_on_home"] = window.AdapterNeedsRestart && window.SetupCard.IsVisible && window.StatusTitle.Text == "等待重启" && Equals(window.StartButton.Content, "查看重启要求");
        checks["restart_requirement_is_not_shown_as_healthy"] = window.DriverStatus.Text.Contains("请重启电脑") && !window.DriverInstallButton.IsEnabled && !window.OriginalBootstrapButton.IsEnabled;
        checks["restart_card_hides_redundant_setup_benefits"] = window.SetupBenefits.Visibility == Visibility.Collapsed;
        window.StartButton.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent)); Pump();
        checks["pending_restart_start_button_does_not_start_receiver"] = !window.SelfTestCommands.Any(command => command.Text("command") == "start");
        window.ShowPage("home"); Capture(window, Path.Combine(folder, "restart-required.png"));
        window.ApplyDriverInventory([restartAdapter with { RebootRequired = false, Healthy = true }]); Pump();
        checks["cleared_restart_restores_normal_start"] = !window.AdapterNeedsRestart && !window.SetupCard.IsVisible && Equals(window.StartButton.Content, "开始接收");
        checks["adapter_choice_always_precedes_mode_even_for_one_adapter"] = window.DriverChoiceRow.Visibility == Visibility.Visible && window.DriverPicker.Items.Count == 1 && window.DriverChoiceRow.TranslatePoint(new(0,0),window.BluetoothSetupCard).Y < window.ModePicker.TranslatePoint(new(0,0),window.BluetoothSetupCard).Y;
        checks["labels_distinguish_mobile_devices_and_computer_inputs"] = window.PlaybackGain.Label == "设备声音" && window.MicrophoneGain.Label == "电脑麦克风" && window.SystemGain.Label == "电脑声音";
        checks["codec_and_playback_mode_share_home_row"] = window.CodecPicker.IsVisible && window.PlaybackModePicker.IsVisible && Math.Abs(window.CodecPicker.TranslatePoint(new(0,0),window).Y - window.PlaybackModePicker.TranslatePoint(new(0,0),window).Y) < 2;
        checks["media_selector_and_automatic_voice_codec_are_distinguished"] = window.MediaCodecLabel.Text == "媒体编码" && window.VoiceCodecHint.Text.StartsWith("语音编码：自动协商");
        checks["mix_switch_is_on_home_and_off_means_two_controls"] = window.SystemAudioToggle.IsVisible && window.SystemGain.Visibility == Visibility.Collapsed;
        Capture(window, Path.Combine(folder, "home-empty.png"));
        checks["all_four_context_help_actions_are_visible"] = window.UsageHelpButton.IsVisible && window.PlaybackHelpButton.IsVisible && window.CodecHelpButton.IsVisible && window.SystemAudioHelpButton.IsVisible;
        Click(window.UsageHelpButton);
        checks["usage_help_explains_owner_features_and_switch_without_repeating_voice_guard"] = window.AudioOptionHelpPopup.IsOpen && window.AudioOptionHelpTitle.Text == "蓝牙使用方式" && window.OptionHelpPlainText.Contains("多设备同时连接") && window.OptionHelpPlainText.Contains("管理员授权") && !window.OptionHelpPlainText.Contains("通话／语音保护");
        Capture((FrameworkElement)window.AudioOptionHelpPopup.Child, Path.Combine(folder, "help-usage-popup.png"));
        Click(window.CodecHelpButton);
        checks["codec_help_opens_from_icon_and_replaces_previous_topic"] = window.AudioOptionHelpPopup.IsOpen && window.AudioOptionHelpTitle.Text == "媒体编码" && window.OptionHelpPlainText.Contains("语音编码") && window.OptionHelpPlainText.Contains("重新连接");
        Capture((FrameworkElement)window.AudioOptionHelpPopup.Child, Path.Combine(folder, "help-codec-popup.png"));
        Click(window.CodecHelpButton);
        checks["clicking_same_help_icon_closes_popup"] = !window.AudioOptionHelpPopup.IsOpen;
        window.ShowPage("settings"); window.MaximumPicker.SelectedIndex = 3; window.FlushSettings().GetAwaiter().GetResult(); window.ShowPage("home");
        Click(window.PlaybackHelpButton);
        checks["playback_help_shows_saved_connection_limit_and_current_count"] = window.AudioOptionHelpPopup.IsOpen && window.AudioOptionHelpTitle.Text == "多设备播放" && window.AudioOptionHelpFact.IsVisible && window.AudioOptionHelpFactText.Text.Contains("最多同时连接 4 台设备") && window.AudioOptionHelpFactText.Text.Contains("当前已连接 0 台");
        Capture((FrameworkElement)window.AudioOptionHelpPopup.Child, Path.Combine(folder, "help-playback-popup.png"));
        Click(window.CloseOptionHelpButton);
        checks["help_close_button_works"] = !window.AudioOptionHelpPopup.IsOpen;
        window.ShowPage("settings"); window.MaximumPicker.SelectedIndex = 2; window.FlushSettings().GetAwaiter().GetResult(); window.ShowPage("home");
        Click(window.SystemAudioHelpButton);
        checks["system_sound_help_explains_target_mix_gains_and_return_exclusion"] = window.AudioOptionHelpPopup.IsOpen && window.AudioOptionHelpTitle.Text == "同时发送电脑声音" && window.OptionHelpPlainText.Contains("当前使用蓝牙麦克风") && window.OptionHelpPlainText.Contains("麦克风发送") && window.OptionHelpPlainText.Contains("排除 BlueHead");
        Capture((FrameworkElement)window.AudioOptionHelpPopup.Child, Path.Combine(folder, "help-system-popup.png"));
        window.ShowPage("settings");
        checks["navigating_away_closes_context_help"] = !window.AudioOptionHelpPopup.IsOpen;
        window.ShowPage("home"); window.Hide(); window.WindowState = WindowState.Minimized;
        window.TrayMouseClick(null, new System.Windows.Forms.MouseEventArgs(System.Windows.Forms.MouseButtons.Right, 1, 0, 0, 0)); Pump();
        checks["tray_right_click_does_not_unhide_main_window"] = !window.IsVisible;
        window.TrayMouseClick(null, new System.Windows.Forms.MouseEventArgs(System.Windows.Forms.MouseButtons.Left, 1, 0, 0, 0)); Pump();
        checks["tray_single_left_click_shows_and_restores_main_window"] = window.IsVisible && window.WindowState == WindowState.Normal;

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
        window.FlushSettings().GetAwaiter().GetResult();
        checks["global_codec_preference_can_save_during_voice_without_restarting"] = window.CodecPicker.IsEnabled && window.SelfTestCommands.Last().Text("command") == "preferences";
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
        Click(window.PlaybackHelpButton);
        var helpCount = window.State.Devices.Count;
        Event("""{"event":"device","key":"help-count-check","state":"已连接"}"""); Pump();
        checks["open_playback_help_updates_current_connection_count"] = window.AudioOptionHelpPopup.IsOpen && window.AudioOptionHelpFactText.Text.Contains($"当前已连接 {helpCount + 1} 台");
        Event("""{"event":"device","key":"help-count-check","state":"已断开"}"""); Pump();
        Click(window.CloseOptionHelpButton);

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
        Pump(350);
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
        Event(JsonSerializer.Serialize(new { @event = "audio_device", direction = "output", device = 1, request_id = window.SelfTestCommands.Last(item => item.Text("command") == "audio_device").Number("request_id"), success = true }));
        checks["output_switch_ack_keeps_connection"] = window.OutputPicker.IsEnabled && window.State.Devices.Count == 1;
        window.InputPicker.SelectedIndex = 1;
        Event(JsonSerializer.Serialize(new { @event = "audio_device", direction = "input", device = 2, request_id = window.SelfTestCommands.Last(item => item.Text("command") == "audio_device").Number("request_id"), success = false, message = "测试：麦克风不可用，已保留原设备" }));
        checks["failed_input_switch_rolls_back"] = window.InputPicker.IsEnabled && window.InputPicker.SelectedIndex == 0 && window.State.Devices.Count == 1;
        Event("""{"event":"call","key":"demo-phone","state":"来电","phase":"incoming"}""");
        checks["incoming_call_actions"] = window.State.Devices[0].CanAnswer && window.State.Devices[0].CanHangup;
        Event("""{"event":"call","key":"demo-phone","state":"通话中","phase":"active"}""");
        checks["active_call_actions"] = !window.State.Devices[0].CanAnswer && window.State.Devices[0].CanHangup;
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-phone","media":{"codec":"aptx_hd","rate":48000,"bitrate":576000,"packets_per_second":100,"playing":true,"buffer_ms":60,"recoveries":2},"call":{}}]}""");
        checks["live_media_protocol_rate_measured_bitrate"] = window.State.Devices[0].MediaDescription.Contains("aptX HD") && window.State.Devices[0].MediaDescription.Contains("576 kb/s") && window.State.Devices[0].LiveDetails.Contains("重同步 2");
        checks["native_rate_details_show_reported_rate"] = window.State.Devices[0].LiveDetails.Contains("100 包/s") && window.State.Devices[0].LiveDetails.Contains("实际缓冲 60 ms");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-phone","media":{"codec":"aac","rate":48000,"playing":true},"call":{}}]}""");
        checks["unavailable_counters_are_not_fabricated_as_zero"] = !window.State.Devices[0].LiveDetails.Contains("包/s") && !window.State.Devices[0].LiveDetails.Contains("实际缓冲");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-phone","media":{},"call":{"active":true,"codec":"msbc","rate":16000,"rx_bitrate":60800,"tx_bitrate":60800,"rx_packets":160,"microphone_active":true,"system_audio_active":true}}]}""");
        checks["call_uses_same_live_protocol_line"] = window.State.Devices[0].MediaDescription.Contains("HFP / mSBC") && window.State.Devices[0].MediaDescription.Contains("60.8 kb/s") && window.State.Devices[0].LiveDetails.Contains("电脑声音已加入");
        window.PlaybackGain.Value = 180; window.MicrophoneGain.Value = 160;
        window.State.Devices[0].Volume = 175; window.State.Devices[0].UplinkVolume = 130;
        checks["independent_device_and_master_levels_0_to_200"] = window.PlaybackGain.Maximum == 200 && window.MicrophoneGain.Maximum == 200 && window.SystemGain.Maximum == 200 && window.State.Devices[0].Volume == 175 && window.State.Devices[0].UplinkVolume == 130;
        window.SystemAudioToggle.IsChecked = true;
        Event(JsonSerializer.Serialize(new { @event = "system_audio", success = true, enabled = true, request_id = window.SelfTestCommands.Last(item => item.Text("command") == "system_audio").Number("request_id") }));
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
        checks["computer_source_selection_is_next_to_its_gain"] = window.SystemPicker.IsVisible && window.State.SystemSources.Count >= 2;
        window.State.SystemSources[1] = new(1, "电脑耳机（界面示例）", "demo-output");
        window.SystemPicker.SelectedIndex = 1; Pump();
        var sourceChoice = window.SelfTestCommands.Last();
        checks["computer_source_switch_uses_stable_endpoint_and_keeps_bluetooth"] = sourceChoice.Text("command") == "audio_device" && sourceChoice.Text("direction") == "system" && sourceChoice.Text("endpoint_id") == "demo-output";
        Event(JsonSerializer.Serialize(new { @event = "audio_device", direction = "system", device = 1, endpoint_id = "demo-output", request_id = sourceChoice.GetProperty("request_id").GetInt32(), success = true }));
        checks["computer_source_ack_commits_selection"] = window.SystemPicker.SelectedIndex == 1 && window.SystemPicker.IsEnabled;
        window.SystemPicker.SelectedIndex = 0; Pump();
        var allSources = window.SelfTestCommands.Last();
        Event(JsonSerializer.Serialize(new { @event = "audio_device", direction = "system", device = (int?)null, endpoint_id = (string?)null, request_id = allSources.GetProperty("request_id").GetInt32(), success = true }));
        checks["computer_source_default_excludes_bluehead"] = window.SystemPicker.SelectedIndex == 0 && window.SystemGain.Secondary.Contains("排除 BlueHead");
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
            var firstGainCommand = window.SelfTestCommands.Count; down.ToggleSilence();
            checks["device_mute_sends_zero_to_matching_engine_route"] = WaitForCommand(firstGainCommand, command => command.Text("command") == "gain" && command.Text("key") == "demo-phone" && command.Text("direction") == "down" && command.Number("gain") == 0);
            firstGainCommand = window.SelfTestCommands.Count; down.ToggleSilence();
            checks["device_unmute_sends_restored_gain"] = WaitForCommand(firstGainCommand, command => command.Text("command") == "gain" && command.Text("key") == "demo-phone" && command.Text("direction") == "down" && command.Number("gain") == 1.75);
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
        var firstMasterCommand = window.SelfTestCommands.Count;
        window.PlaybackGain.Value = 145; window.PlaybackGain.ToggleSilence();
        checks["master_mute_sends_zero_to_engine"] = WaitForCommand(firstMasterCommand, command => command.Text("command") == "master_gain" && command.Text("bus") == "playback" && command.Number("gain") == 0);
        firstMasterCommand = window.SelfTestCommands.Count; window.PlaybackGain.ToggleSilence();
        checks["master_unmute_sends_restored_gain"] = WaitForCommand(firstMasterCommand, command => command.Text("command") == "master_gain" && command.Text("bus") == "playback" && command.Number("gain") == 1.45);
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
        Event("""{"event":"audio_health","direction":"output","state":"warning","message":"播放设备暂不可用，后台将稍后重试"}""");
        checks["audio_recovery_explains_pending_retry"] = window.Notice.IsVisible && window.NoticeText.Text.Contains("稍后重试");
        Event("""{"event":"warning","message":"独立测试提示"}""");
        Event("""{"event":"audio_health","direction":"output","state":"restored","message":"播放设备已恢复"}""");
        checks["audio_recovery_does_not_clear_an_unrelated_notice"] = window.Notice.IsVisible && window.NoticeText.Text == "独立测试提示";
        Event("""{"event":"audio_health","direction":"microphone","state":"warning","message":"麦克风访问被拒绝，请检查权限"}""");
        Event("""{"event":"audio_health","direction":"microphone","state":"restored","message":"麦克风已恢复"}""");
        checks["audio_recovery_clears_its_own_notice"] = !window.Notice.IsVisible;
        checks["audio_recovery_reports_restored_route"] = window.FooterStatus.Text == "麦克风已恢复";
        Event("""{"event":"stopped"}""");
        checks["stop_clears_connections"] = window.State.Devices.Count == 0 && window.AdapterPicker.IsEnabled;
        Event("""{"event":"call","key":"demo-phone","state":"通话服务已断开，可重连"}""");
        Event("""{"event":"microphone","key":"demo-phone","active":false}""");
        checks["late_events_do_not_resurrect_disconnected_devices"] = window.State.Devices.Count == 0;
        window.ShowPage("home");
        Event("""{"event":"starting"}""");
        window.CodecPicker.SelectedItem = window.CodecPicker.Items.OfType<System.Windows.Controls.ComboBoxItem>().First(item => Equals(item.Tag, "aac"));
        Event("""{"event":"audio_refreshing"}""");
        var beforeRefreshApply = window.SelfTestCommands.Count;
        window.ApplyCodecProfile().GetAwaiter().GetResult();
        checks["codec_preferences_during_audio_refresh_never_restart_controller"] = window.SelfTestCommands.Skip(beforeRefreshApply).All(item => item.Text("command") == "preferences");
        Event("""{"event":"audio_refreshed","success":true,"output_device":null,"input_device":null,"output_endpoint_id":"recovery-preferred","input_endpoint_id":null}""");
        checks["codec_preferences_use_compact_settings_instead_of_restart_button"] = window.CodecSettingsButton.IsVisible;
        window.ApplyCodecProfile().GetAwaiter().GetResult();
        checks["applying_global_codec_preserves_live_receiver"] = window.SelfTestCommands.Last().Text("command") == "preferences" && window.StartButton.Content.ToString() == "停止接收";
        var savedCodec = window.SelfTestCommands.Last().GetProperty("values");
        checks["codec_preferences_do_not_overwrite_preferred_endpoint_intent"] = !savedCodec.TryGetProperty("output_endpoint_id", out _);
        checks["codec_preferences_do_not_overwrite_audio_gains_or_playback_mode"] = savedCodec.Text("codec") == "aac" && !savedCodec.TryGetProperty("playback_gain", out _) && !savedCodec.TryGetProperty("media_mode", out _);
        Event("""{"event":"stopped"}""");
        Event("""{"event":"starting"}""");
        Event("""{"event":"device","key":"demo-a","state":"已连接"}""");
        Event("""{"event":"device","key":"demo-b","state":"已连接"}""");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"demo-a","media":{"codec":"aac","rate":48000,"bitrate":180000,"protected":true,"lost_packets":123},"call":{}}]}""");
        checks["protection_is_visible_with_global_parameters_and_reconnect_action"] = window.State.Devices[0].PlaybackNotice.Contains("隔离") && window.MultipointText.Text.Contains("丢包") && window.MultipointText.Text.Contains("重连") && window.LowerBitrateButton.IsVisible;
        Capture(window, Path.Combine(folder, "home-protection-demo.png"));
        Event("""{"event":"stopped"}""");


        window.ShowPage("help"); Capture(window, Path.Combine(folder, "help.png"));
        checks["brand_and_help_expose_same_requested_github_repository"] = window.BrandProjectLink.NavigateUri.AbsoluteUri == "https://github.com/tingol666/BlueHead" && window.HelpProjectLink.NavigateUri == window.BrandProjectLink.NavigateUri;
        checks["brand_project_link_fits_compact_navigation"] = window.BrandProjectLink.Parent is System.Windows.Controls.TextBlock brand && brand.ActualWidth + brand.TranslatePoint(new(0,0),window).X <= 145;
        checks["advanced_settings_secondary"] = window.HomePage.Visibility != Visibility.Visible;
        Event("""{"event":"inventory","adapters":[],"audio":[]}""");
        window.ShowPage("home");
        checks["removed_adapter_returns_to_setup"] = window.SetupCard.IsVisible && window.NeedsBluetoothSetup;
        Event("""{"event":"stopped"}""");
        var systemAdapter = restartAdapter with { Service = "BTHUSB", RebootRequired = false, Healthy = true, HasBackup = true, SystemRadioReady = true };
        var dedicatedAdapter = restartAdapter with { RebootRequired = false, Healthy = true, HasBackup = true };
        int Starts() => window.SelfTestCommands.Count(command => command.Text("command") == "start");
        string SavedMode() => window.SelfTestCommands.Last(command => command.Text("command") == "configure").GetProperty("settings").Text("mode");
        window.ApplyDriverInventory([systemAdapter], true);
        window.ShowPage("adapters"); window.ModePicker.SelectedIndex = 1; Pump();
        checks["ready_system_driver_allows_basic_mode_without_uac"] = !window.HasPendingMode && SavedMode() == "native" && window.SelfTestDriverOperations.Count == 0;
        checks["home_and_driver_mode_selectors_share_effective_value"] = window.ModePicker.SelectedIndex == 1 && window.HomeModePicker.SelectedIndex == 1;
        checks["mode_switch_clears_unrelated_previous_mode_footer"] = window.FooterStatus.Text == "本机蓝牙 · 无需同一 Wi-Fi";
        checks["system_driver_hides_restore_and_repair_in_basic_mode"] = window.DriverRestoreButton.Visibility == Visibility.Collapsed && window.OriginalBootstrapButton.Visibility == Visibility.Collapsed && window.DriverInstallButton.Visibility == Visibility.Collapsed;
        Capture(window, Path.Combine(folder, "driver-basic-ready.png"));
        window.ShowPage("home"); Pump();
        checks["basic_mode_explains_exact_computer_and_mobile_steps"] = window.HomeNextStep.Text.Contains("电脑") && window.HomeNextStep.Text.Contains("Windows 蓝牙设置") && window.HomeNextStep.Text.Contains("BlueHead") && window.HomeNextStep.Text.Contains("开始接收") && window.HomeNextStep.Text.Contains("手机／平板播放");
        checks["basic_mode_has_direct_windows_pairing_action"] = window.HomePairButton.IsVisible;
        checks["basic_mode_keeps_only_applicable_audio_controls"] = window.SharedAudioFooter.IsVisible && window.DedicatedAudioFooter.Visibility == Visibility.Collapsed && window.MediaProfileCard.Visibility == Visibility.Collapsed;
        checks["windows_managed_settings_disabled_in_shared_mode"] = !window.NameEntry.IsEnabled && !window.CallsToggle.IsEnabled && !window.CodecPicker.IsEnabled;
        checks["shared_mode_does_not_advertise_dedicated_voice_negotiation"] = window.VoiceCodecHint.Text.Contains("不提供蓝牙语音") && !window.VoiceCodecHint.Text.Contains("自动协商");
        Capture(window, Path.Combine(folder, "shared-mode.png"));
        Click(window.HomePairButton);
        checks["basic_pairing_button_opens_windows_bluetooth"] = window.SelfTestExternalLinks.Last() == "ms-settings:bluetooth";
        window.ShowPage("settings"); Pump();
        checks["shared_settings_explain_advanced_features_instead_of_dead_controls"] = window.SharedSettingsNotice.IsVisible && window.SharedNameFields.IsVisible && window.AdvancedAudio.Visibility == Visibility.Collapsed;
        window.ShowPage("adapters"); window.ModePicker.SelectedIndex = 0; Pump();
        checks["unprepared_mode_is_a_draft_not_an_effective_setting"] = window.HasPendingMode && window.RequestedMode == "advanced" && window.ModePicker.SelectedIndex == 1 && window.HomeModePicker.SelectedIndex == 1 && SavedMode() == "native";
        checks["unprepared_mode_states_next_step_and_driver_impact"] = window.ModePendingTitle.Text.Contains("待你确认") && window.ChoiceFeedback.Text.Contains("尚未切换") && window.ModePendingText.Text.Contains("返回或取消") && window.DriverImpactTitle.Text.Contains("鼠标") && window.DriverImpactTitle.Text.Contains("断开") && window.DriverImpactText.Text.Contains("Windows") && window.DriverImpactText.Text.Contains("蓝牙开关会消失");
        checks["system_driver_offers_only_backup_install_for_advanced_choice"] = window.DriverInstallButton.IsVisible && window.DriverInstallButton.IsEnabled && window.DriverRestoreButton.Visibility == Visibility.Collapsed && window.OriginalBootstrapButton.Visibility == Visibility.Collapsed;
        var operationsBeforeDraft = window.SelfTestDriverOperations.Count;
        window.FlushSettings().GetAwaiter().GetResult();
        checks["saving_settings_cannot_persist_an_unprepared_mode"] = SavedMode() == "native" && window.HasPendingMode && window.SelfTestDriverOperations.Count == operationsBeforeDraft;
        window.ApplyDriverInventory([systemAdapter], true); Pump();
        checks["refresh_does_not_commit_a_pending_mode"] = window.HasPendingMode && window.HomeModePicker.SelectedIndex == 1;
        Capture(window, Path.Combine(folder, "driver-advanced-pending.png"));
        window.ShowPage("home"); Pump();
        checks["leaving_driver_page_discards_unfinished_mode_choice"] = !window.HasPendingMode && window.ModePicker.SelectedIndex == 1 && SavedMode() == "native";
        window.HomeModePicker.SelectedIndex = 0; Pump();
        checks["home_advanced_choice_opens_driver_preparation_without_uac"] = window.AdaptersPage.IsVisible && window.HasPendingMode && window.HomeModePicker.SelectedIndex == 1 && window.SelfTestDriverOperations.Count == operationsBeforeDraft;
        Click(window.FinishBluetoothButton);
        checks["return_home_discards_draft_without_starting_receiver"] = window.HomePage.IsVisible && !window.HasPendingMode && SavedMode() == "native";
        window.HomeModePicker.SelectedIndex = 0; Pump();
        var modeSwitchStarts = Starts();
        window.SelfTestDriverRunner = (_, _) => Task.FromException<(int, DriverRecord?)>(new System.ComponentModel.Win32Exception(1223));
        Click(window.DriverInstallButton);
        checks["cancelled_install_preserves_basic_mode_and_driver"] = !window.HasPendingMode && window.ModePicker.SelectedIndex == 1 && SavedMode() == "native" && window.NoticeText.Text.Contains("已取消切换");
        window.ModePicker.SelectedIndex = 0; Pump();
        window.SelfTestDriverRunner = (_, device) => Task.FromResult((1, (DriverRecord?)new DriverRecord { InstanceId = device.InstanceId, Stage = "failed", Error = "device busy" }));
        Click(window.DriverInstallButton);
        checks["busy_driver_has_specific_close_refresh_and_restart_steps"] = window.DriverResult.Text.Contains("占用") && window.DriverResult.Text.Contains("关闭") && window.DriverResult.Text.Contains("刷新状态") && window.DriverResult.Text.Contains("重启电脑后重试") && window.HasPendingMode && SavedMode() == "native";
        var pendingInstall = new TaskCompletionSource<(int ExitCode, DriverRecord? Record)>();
        window.SelfTestDriverRunner = (_, _) => pendingInstall.Task;
        Click(window.DriverInstallButton);
        checks["driver_install_shows_permission_then_working_progress"] = window.SelfTestDriverActivities.Any(item => item.Operation == "install" && item.Waiting && item.Title.Contains("管理员授权")) && window.DriverActivityNotice.IsVisible && window.DriverActivityTitle.Text.Contains("正在备份原驱动") && !window.CancelModeButton.IsEnabled && !window.ModePicker.IsEnabled;
        Capture(window, Path.Combine(folder, "driver-install-working.png"));
        window.ApplyDriverInventory([dedicatedAdapter], false);
        pendingInstall.SetResult((0, new DriverRecord { InstanceId = dedicatedAdapter.InstanceId, Stage = "winusb_ready" })); Pump();
        Event("""{"event":"inventory","adapters":[{"transport":"usb:1234:abcd#0","name":"专用适配器（示例）"}],"audio":[]}"""); Pump();
        checks["verified_install_commits_advanced_mode_without_starting"] = !window.HasPendingMode && SavedMode() == "advanced" && window.ModePicker.SelectedIndex == 0 && window.HomeModePicker.SelectedIndex == 0 && Starts() == modeSwitchStarts;
        checks["dedicated_driver_hides_install_and_groups_restore_repair"] = window.DriverInstallButton.Visibility == Visibility.Collapsed && window.DriverRestoreButton.IsVisible && window.DriverRestoreButton.IsEnabled && window.OriginalBootstrapButton.IsVisible && window.OriginalBootstrapButton.IsEnabled;
        checks["actual_driver_status_is_separate_from_selected_mode"] = window.DriverStateTitle.Text.Contains("专用") && window.EffectiveModeLabel.Text.Contains("BlueHead 专用蓝牙");
        Capture(window, Path.Combine(folder, "driver-dedicated-ready.png"));
        window.ModePicker.SelectedIndex = 1; Pump();
        checks["basic_choice_on_dedicated_driver_remains_pending"] = window.HasPendingMode && SavedMode() == "advanced" && window.HomeModePicker.SelectedIndex == 0 && window.DriverRestoreButton.IsVisible && window.DriverInstallButton.Visibility == Visibility.Collapsed;
        checks["restore_preview_explains_restoration_instead_of_repeating_install_warning"] = window.ModePendingTitle.Text.Contains("恢复 Windows 共用蓝牙") && window.DriverImpactTitle.Text.Contains("系统蓝牙可用") && window.DriverImpactText.Text.Contains("不再通过此适配器发送电脑麦克风") && window.DriverImpactTitle.Foreground is SolidColorBrush restoreBrush && restoreBrush.Color.B > restoreBrush.Color.R && window.OriginalBootstrapButton.Visibility == Visibility.Collapsed;
        Capture(window, Path.Combine(folder, "shared-restore-required.png"));
        window.ShowPage("home"); Pump();
        checks["pending_restore_cannot_change_home_effective_mode"] = !window.HasPendingMode && window.HomeModePicker.SelectedIndex == 0 && !window.SharedAudioFooter.IsVisible;
        window.ShowPage("adapters"); window.ModePicker.SelectedIndex = 1; Pump();
        window.SelfTestDriverRunner = (_, _) => Task.FromException<(int, DriverRecord?)>(new System.ComponentModel.Win32Exception(1223));
        Click(window.DriverRestoreButton);
        checks["cancelled_restore_preserves_advanced_mode"] = !window.HasPendingMode && window.ModePicker.SelectedIndex == 0 && SavedMode() == "advanced";
        window.ModePicker.SelectedIndex = 1; Pump();
        window.SelfTestDriverRunner = (_, device) =>
        {
            window.ApplyDriverInventory([systemAdapter], true);
            return Task.FromResult((0, (DriverRecord?)new DriverRecord { InstanceId = device.InstanceId, Stage = "restored" }));
        };
        Click(window.DriverRestoreButton);
        checks["verified_restore_commits_basic_mode_without_starting"] = !window.HasPendingMode && SavedMode() == "native" && window.ModePicker.SelectedIndex == 1 && Starts() == modeSwitchStarts && window.AdaptersPage.IsVisible;
        checks["restored_system_driver_cannot_be_restored_again"] = window.DriverRestoreButton.Visibility == Visibility.Collapsed && window.OriginalBootstrapButton.Visibility == Visibility.Collapsed;
        Capture(window, Path.Combine(folder, "shared-restored.png"));
        window.Width = window.MinWidth; window.Height = window.MinHeight; Pump(); Capture(window, Path.Combine(folder, "shared-restored-minimum.png"));
        checks["driver_steps_remain_ordered_at_minimum_size"] = window.DriverPicker.TranslatePoint(new(0,0),window.BluetoothSetupCard).Y < window.ModePicker.TranslatePoint(new(0,0),window.BluetoothSetupCard).Y && window.ModePicker.TranslatePoint(new(0,0),window.BluetoothSetupCard).Y < window.DriverStateTitle.TranslatePoint(new(0,0),window.BluetoothSetupCard).Y;
        checks["driver_operations_are_grouped_on_the_right"] = window.DriverActions.HorizontalAlignment == HorizontalAlignment.Right;
        checks["primary_driver_actions_stay_visible_without_scrolling"] = window.DriverActions.TranslatePoint(new(0,window.DriverActions.ActualHeight),window).Y < window.ActualHeight - 16 && window.FinishBluetoothButton.IsVisible && window.DriverStateTitle.IsVisible;
        window.ApplyDriverInventory([dedicatedAdapter], false); window.ModePicker.SelectedIndex = 0; Pump(); window.ModePicker.SelectedIndex = 1; Pump();
        checks["pending_restore_and_its_consequences_stay_with_actions"] = window.ModePendingNotice.IsVisible && window.DriverImpact.IsVisible && window.CancelModeButton.IsVisible && window.DriverImpact.TranslatePoint(new(0,window.DriverImpact.ActualHeight),window).Y < window.DriverActions.TranslatePoint(new(0,0),window).Y && window.DriverActions.TranslatePoint(new(0,window.DriverActions.ActualHeight),window).Y < window.ActualHeight - 16;
        Click(window.CancelModeButton);
        checks["explicit_cancel_restores_both_visible_mode_choices"] = !window.HasPendingMode && window.HomeModePicker.SelectedIndex == 0 && window.ModePicker.SelectedIndex == 0;
        window.ApplySuccessfulDriverOperation("restore", dedicatedAdapter);

        checks["driver_page_scrolls_without_clipping_controls"] = window.AdaptersScroll.ScrollableHeight > 0 && window.AdaptersScroll.ViewportHeight > 100;
        window.ApplyDriverInventory([systemAdapter with { RebootRequired = true, SystemRadioReady = false }], false); Pump();
        checks["pending_restart_hides_install_and_restore"] = window.DriverInstallButton.Visibility == Visibility.Collapsed && window.DriverRestoreButton.Visibility == Visibility.Collapsed && window.OriginalBootstrapButton.Visibility == Visibility.Collapsed && window.DriverActionHint.Text.Contains("重启");
        checks["restart_requirement_is_also_visible_on_home"] = window.AdapterNeedsRestart && window.SetupCard.Visibility == Visibility.Visible;
        WindowSizing.Apply(window, true);
        window.ApplyDriverInventory([systemAdapter], true); window.ShowPage("home");
        Event("""{"event":"starting"}""");
        window.HomeModePicker.SelectedIndex = 0; Pump();
        checks["running_mode_choice_is_disabled_and_programmatic_change_reverted"] = window.HomeModePicker.SelectedIndex == 1 && !window.HomeModePicker.IsEnabled && !window.ModePicker.IsEnabled && !window.HasPendingMode;
        Event("""{"event":"stopped"}""");
        window.ApplyDriverInventory([dedicatedAdapter], false); window.ShowPage("adapters"); window.ModePicker.SelectedIndex = 0; Pump();
        checks["matching_dedicated_driver_applies_mode_without_reinstall"] = SavedMode() == "advanced" && !window.HasPendingMode;
        var otherDedicated = dedicatedAdapter with { InstanceId = @"USB\VID_1234&PID_5678\other", Name = "其他适配器（示例）" };
        window.ApplyDriverInventory([dedicatedAdapter, otherDedicated], false);
        window.ApplySuccessfulDriverOperation("restore", otherDedicated);
        checks["restoring_another_adapter_keeps_effective_receiver_mode"] = window.ModePicker.SelectedIndex == 0;
        window.ApplyDriverInventory([dedicatedAdapter], false);
        window.ShowPage("home");
        Event("""{"event":"error","category":"missing_firmware","message":"missing firmware","firmware_profile":"intel-tlv-0041"}"""); Pump();
        checks["firmware_failure_has_home_driver_action"] = window.NeedsBluetoothSetup && window.SetupCard.IsVisible && window.NoticeAction.IsVisible;
        Click(window.NoticeAction);
        checks["firmware_actions_only_appear_when_needed"] = window.ControllerPreparation.IsVisible && window.AcquireFirmwareButton.IsEnabled && window.ImportFirmwareButton.IsEnabled;
        Capture(window, Path.Combine(folder, "firmware-required-adapter.png"));
        Event("""{"event":"controller_capabilities","voice_transport":true}""");
        window.ShowPage("home"); Click(window.StartButton);
        Event("""{"event":"starting"}""");
        Event("""{"event":"device","key":"lost-controller-demo","state":"已连接"}""");
        Event("""{"event":"error","category":"le_audio_stream","message":"音频通道未能准备，请断开并重新连接这个设备。"}""");
        checks["le_stream_failure_does_not_request_driver_repair"] = window.CurrentControllerGuide == null && !window.ControllerNeedsInitialization && window.NoticeText.Text.Contains("音频通道");
        Event("""{"event":"warning","category":"le_audio_connection_changed","message":"连接已变化"}""");
        checks["le_connection_change_does_not_request_driver_repair"] = window.CurrentControllerGuide == null && !window.ControllerNeedsInitialization;
        var previousDriverOperations = window.SelfTestDriverOperations.Count;
        Event("""{"event":"error","category":"controller_hardware_error","message":"Controller error"}""");
        Event("""{"event":"stopping"}""");
        checks["controller_failure_clears_ghost_devices_promptly"] = window.State.Devices.Count == 0;
        Event("""{"event":"stopped"}"""); Pump();
        checks["controller_failure_never_opens_uac"] = window.SelfTestDriverOperations.Count == previousDriverOperations && window.NoticeText.Text.Contains("由你确认") && window.NoticeAction.IsVisible;
        Click(window.NoticeAction);
        checks["fault_page_offers_named_repair_action_without_background_uac"] = window.AdaptersPage.IsVisible && window.OriginalBootstrapButton.IsVisible && window.OriginalBootstrapButton.IsEnabled && window.SelfTestDriverOperations.Count == previousDriverOperations;
        checks["fault_page_hides_unneeded_firmware_download"] = window.ControllerPreparation.Visibility == Visibility.Collapsed && window.DriverInstallButton.Visibility == Visibility.Collapsed;
        Capture(window, Path.Combine(folder, "controller-recovery.png"));
        window.SelfTestDriverRunner = (_, _) => Task.FromException<(int, DriverRecord?)>(new System.ComponentModel.Win32Exception(1223));
        Click(window.OriginalBootstrapButton);
        checks["cancelled_repair_keeps_manual_action_available"] = window.NoticeText.Text.Contains("已取消恢复") && window.OriginalBootstrapButton.IsEnabled && window.ControllerNeedsInitialization;
        window.SelfTestDriverRunner = (_, device) => Task.FromResult((0, (DriverRecord?)new DriverRecord { InstanceId = device.InstanceId, Stage = "winusb_ready" }));
        var recoveryStarts = Starts();
        Click(window.OriginalBootstrapButton);
        checks["verified_repair_does_not_start_reception"] = Starts() == recoveryStarts && window.AdaptersPage.IsVisible && !window.ControllerNeedsInitialization && window.SelfTestDriverOperations.Last() == "initialize-original";
        Click(window.FinishBluetoothButton);
        checks["return_home_does_not_start_reception"] = window.HomePage.IsVisible && Starts() == recoveryStarts;
        Click(window.StartButton);
        checks["only_home_start_begins_reception"] = Starts() == recoveryStarts + 1;
        Event("""{"event":"voice_protection","state":"active","message":"通话／语音保护已开启：暂时只连接使用电脑麦克风的设备。"}"""); Pump();
        checks["voice_protection_is_default_and_explained_on_home"] = window.VoiceProtectionToggle.IsChecked == true && !window.VoiceProtectionToggle.IsEnabled && window.VoiceProtectionNotice.IsVisible && window.VoiceProtectionText.Text.Contains("电脑麦克风");
        Event("""{"event":"voice_protection","state":"blocked","message":"有其它设备尝试连接。当前设备正在使用电脑麦克风，为避免蓝牙中断，已暂时限制为单设备连接。"}""");
        checks["blocked_connection_notice_avoids_codec_jargon"] = window.VoiceProtectionText.Text.Contains("其它设备尝试连接") && !window.VoiceProtectionText.Text.Contains("mSBC");
        Event("""{"event":"voice_protection","state":"released","message":"通话／语音保护已结束，可以重新连接其它设备。"}""");
        checks["voice_end_explains_reconnection"] = window.VoiceProtectionText.Text.Contains("可以重新连接");
        Event("""{"event":"stopped"}""");
        checks["stop_clears_voice_protection_banner"] = window.VoiceProtectionNotice.Visibility == Visibility.Collapsed && window.VoiceProtectionToggle.IsEnabled;
        window.ShowPage("adapters");
        Event("""{"event":"error","category":"controller_unresponsive","message":"Controller still unresponsive"}""");
        window.SelfTestDriverRunner = (_, device) =>
        {
            window.ApplyDriverInventory([systemAdapter with { RebootRequired = true, SystemRadioReady = false }], false);
            return Task.FromResult((3010, (DriverRecord?)new DriverRecord { InstanceId = device.InstanceId, Stage = "restored_restart_required", RebootRequired = true }));
        };
        recoveryStarts = Starts(); Click(window.OriginalBootstrapButton);
        checks["repair_needing_restart_has_exact_instruction_and_no_restart_loop"] = window.AdapterNeedsRestart && Starts() == recoveryStarts && window.NoticeText.Text == "驱动恢复完成，请重启电脑。" && window.OriginalBootstrapButton.Visibility == Visibility.Collapsed;
        window.ApplyDriverInventory([dedicatedAdapter], false); window.ModePicker.SelectedIndex = 0; Pump();
        Event("""{"event":"controller_capabilities","voice_transport":true}""");
        window.SelfTestDriverRunner = null;
        Event("""{"event":"starting"}""");
        Event("""{"event":"status","message":"已就绪：在移动设备蓝牙列表连接 BlueHead PC"}""");
        Event("""{"event":"device","key":"remote-demo","state":"已连接"}""");
        Event("""{"event":"name","key":"remote-demo","name":"平板（媒体控制示例）"}""");
        window.ShowPage("home");
        Event("""{"event":"format","key":"remote-demo","codec":"aac","rate":48000,"channels":2}""");
        var remote = window.State.Devices.Single(); remote.Volume = 175; remote.UplinkVolume = 130;
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":true,"title":"示例曲目","artist":"示例演出者","album":"示例专辑","duration_ms":245000,"position_ms":62000,"playback_status":"playing","volume":0.35}"""); Pump();
        var remoteRow = Visuals<System.Windows.Controls.Grid>(window.DeviceCards).Single(item => item.Name == "RemoteMediaLine");
        var remoteNext = Visuals<Wpf.Ui.Controls.Button>(window.DeviceCards).Single(item => item.Name == "RemoteNext");
        var remotePrevious = Visuals<Wpf.Ui.Controls.Button>(window.DeviceCards).Single(item => item.Name == "RemotePrevious");
        var remotePlay = Visuals<Wpf.Ui.Controls.Button>(window.DeviceCards).Single(item => item.Name == "RemotePlayPause");
        checks["avrcp_metadata_and_controls_use_one_compact_line"] = remoteRow.IsVisible && remoteRow.ActualHeight <= 22 && remoteNext.ActualHeight <= 22 && remoteNext.ActualWidth <= 24 && remote.TrackDescription == "示例曲目 · 示例演出者";
        checks["avrcp_accessible_action_follows_reported_playback_state"] = remotePlay.IsEnabled && Equals(remotePlay.Tag, "pause") && System.Windows.Automation.AutomationProperties.GetName(remotePlay).Contains("暂停") && remote.RemoteDetails.Contains("1:02 / 4:05");
        remoteNext.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Button.ClickEvent));
        checks["avrcp_next_routes_to_the_matching_device"] = window.SelfTestCommands.Last(command => command.Text("command") == "device_action").Text("action") == "next" && window.SelfTestCommands.Last(command => command.Text("command") == "device_action").Text("key") == "remote-demo";
        remotePlay.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Button.ClickEvent));
        checks["avrcp_pause_sends_explicit_action_without_toggling_gain"] = window.SelfTestCommands.Last(command => command.Text("command") == "device_action").Text("action") == "pause" && remote.Volume == 175 && remote.UplinkVolume == 130;
        Capture(window, Path.Combine(folder, "home-avrcp-demo.png"));
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":true,"playback_status":"paused"}"""); Pump();
        checks["avrcp_paused_state_exposes_play"] = remotePlay.IsEnabled && Equals(remotePlay.Tag, "play");
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":true,"playback_status":"unknown"}"""); Pump();
        var actionCount = window.SelfTestCommands.Count;
        remotePlay.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Button.ClickEvent));
        checks["avrcp_unknown_state_never_guesses_play_pause"] = !remotePlay.IsEnabled && remoteNext.IsEnabled && remote.PlaybackHint.Contains("等待") && window.SelfTestCommands.Count == actionCount;
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":["next"],"playback_status":"paused"}"""); Pump();
        checks["avrcp_unsupported_operations_are_disabled"] = remoteNext.IsEnabled && !remotePrevious.IsEnabled && !remotePlay.IsEnabled;
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":false,"standard_volume":0.4}"""); Pump();
        checks["avrcp_standard_volume_fallback_is_separate_from_receive_gain"] = remote.RemoteDetails.Contains("40%") && remote.Volume == 175 && !remoteNext.IsEnabled;
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":true,"playback_status":"playing","volume":0.6,"standard_volume":0.1}""");
        checks["avrcp_volume_field_takes_precedence"] = remote.RemoteDetails.Contains("60%") && remote.Volume == 175 && remote.UplinkVolume == 130;
        Event("""{"event":"avrcp","key":"remote-demo","title":" Not proivde ","artist":"unavailable","album":"Not Provided","position_ms":null,"duration_ms":null}"""); Pump();
        checks["missing_remote_metadata_uses_clear_chinese_without_disabling_controls"] = remote.TrackDescription == "暂无曲目信息" && remote.RemoteDetails.Contains("设备未提供播放进度") && !remote.RemoteDetails.Contains("专辑：") && remote.CanPrevious && remote.CanNext && remote.CanPlayPause && remote.Volume == 175;
        Capture(window, Path.Combine(folder, "home-avrcp-no-metadata.png"));
        Event("""{"event":"avrcp","key":"remote-demo","title":"独立曲目","artist":"","album":"NOT_PROVIDED"}""");
        checks["partial_metadata_has_no_placeholder_album_or_lone_separator"] = remote.TrackDescription == "独立曲目" && !remote.RemoteDetails.Contains("专辑：") && !remote.TrackDescription.Contains(" · ");
        Event("""{"event":"avrcp","key":"remote-demo","title":"Unavailable","artist":"Known Artist","album":""}""");
        checks["real_english_title_with_artist_context_is_preserved"] = remote.TrackDescription == "Unavailable · Known Artist";
        Event("""{"event":"avrcp","key":"remote-demo","title":"Unknown Pleasures","artist":"Unknown Mortal Orchestra","album":"Not Provided (Live)"}""");
        Event("""{"event":"avrcp","key":"remote-demo","playback_status":"paused"}""");
        checks["english_names_and_status_only_updates_preserve_track_metadata"] = remote.TrackDescription == "Unknown Pleasures · Unknown Mortal Orchestra" && remote.RemoteDetails.Contains("Not Provided (Live)") && remote.PlaybackAction == "play";
        Event("""{"event":"avrcp","key":"remote-demo","title":"","artist":"","album":""}""");
        checks["explicit_empty_metadata_replaces_stale_track_with_chinese_hint"] = remote.TrackDescription == "暂无曲目信息";
        Event("""{"event":"avrcp","key":"remote-demo","title":"示例曲目","artist":"示例演出者","playback_status":"playing"}""");
        Event("""{"event":"telemetry","audio":{},"devices":[{"key":"remote-demo","media":{},"call":{"active":true,"codec":"lc3_swb","rate":32000,"rx_bitrate":64000,"tx_bitrate":64000,"microphone_active":true}}]}"""); Pump();
        checks["hfp_lc3_swb_uses_correct_codec_label_and_32khz"] = remote.MediaDescription.Contains("HFP / LC3-SWB · 32 kHz");
        checks["voice_details_preserve_actual_codec_alongside_auto_negotiation"] = remote.LiveDetails.Contains("语音编码：自动协商") && remote.LiveDetails.Contains("当前 LC3-SWB · 32 kHz");
        checks["version_is_consistent_release"] = AppVersion.Number == typeof(Program).Assembly.GetName().Version?.ToString(3)
            && window.Title == "BlueHead " + AppVersion.Number && AppVersion.Label == AppVersion.Number;
        Event("""{"event":"avrcp","key":"remote-demo","connected":false,"controls":false}"""); Pump();
        checks["avrcp_disconnect_clears_metadata_and_reclaims_row"] = !remoteRow.IsVisible && !remote.HasRemoteMedia && !remote.TrackDescription.Contains("示例曲目");
        Event("""{"event":"device","key":"remote-demo","state":"已断开"}""");
        Event("""{"event":"avrcp","key":"remote-demo","connected":true,"controls":true,"title":"late"}""");
        checks["late_avrcp_events_cannot_recreate_disconnected_devices"] = window.State.Devices.Count == 0;
        Event("""{"event":"starting"}""");
        Event("""{"event":"device","key":"lifecycle-a","state":"已连接"}""");
        Event("""{"event":"device","key":"lifecycle-b","state":"已连接"}""");
        Event("""{"event":"telemetry","running":true,"audio":{},"devices":[{"key":"lifecycle-b","media":{},"call":{"active":true,"codec":"msbc","rate":16000}}]}""");
        checks["full_snapshot_removes_stale_connection_card"] = window.State.Devices.Count == 1 && window.State.Devices[0].Key == "lifecycle-b";
        Event("""{"event":"stopping"}""");
        checks["stop_immediately_invalidates_connections_and_disables_driver_changes"] = window.State.Devices.Count == 0 && !window.StartButton.IsEnabled && !window.DriverInstallButton.IsEnabled && !window.DriverRestoreButton.IsEnabled;
        Event("""{"event":"device","key":"late-during-stop","state":"已连接"}""");
        Event("""{"event":"system_audio_state","active":true}""");
        checks["events_during_retirement_cannot_resurrect_devices"] = window.State.Devices.Count == 0;
        Event("""{"event":"stopped"}""");
        checks["completed_stop_restores_start_action"] = window.StartButton.IsEnabled;
        Event("""{"event":"starting"}""");
        Event("""{"event":"device","key":"new-session","state":"已连接"}""");
        using (var stale = JsonDocument.Parse("""{"event":"device","key":"obsolete","state":"已连接"}""")) window.ApplyEngineMessage(-1, stale.RootElement);
        window.ApplyEngineFailure(-1, "obsolete failure");
        checks["queued_old_backend_events_and_faults_do_not_touch_current_session"] = window.State.Devices.Count == 1 && window.State.Devices[0].Key == "new-session";
        window.ApplyEngineFailure(0, "synthetic native failure");
        checks["backend_failure_clears_device_voice_and_pending_session_state"] = window.State.Devices.Count == 0 && window.StartButton.IsEnabled && window.EngineState.Text == "后台已断开";
        Event("""{"event":"starting"}""");
        Event("""{"event":"telemetry","running":true,"audio":{},"devices":[{"key":"reconciled","media":{},"call":{}}]}""");
        checks["full_snapshot_can_recover_a_missing_connection_event"] = window.State.Devices.Count == 1 && window.State.Devices[0].Key == "reconciled";
        Event("""{"event":"telemetry","running":true,"devices_complete":false,"audio":{},"devices":[]}""");
        checks["partial_system_snapshot_does_not_delete_windows_owned_connections"] = window.State.Devices.Count == 1;
        Event("""{"event":"telemetry","running":false,"audio":{},"devices":[]}""");
        checks["stopped_receiver_snapshot_clears_remaining_ui_state"] = window.State.Devices.Count == 0 && window.StartButton.IsEnabled;
        try { ProtocolChecks(window, checks, folder, Event); CodecChecks(window, checks, folder, Event); MediaHealthChecks(window, checks, folder, Event); checks["protocol_checks_completed"] = true; }
        catch (Exception error)
        {
            checks["protocol_checks_completed"] = false;
            File.WriteAllText(Path.Combine(folder, "protocol-check-exception.txt"), error.ToString());
        }
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
            var phase = "prepare";
            var hello = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var inventory = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
            client.Message += (_, message) =>
            {
                if (message.Text("event") == "hello") hello.TrySetResult(true);
                if (message.Text("event") == "inventory") inventory.TrySetResult(message.GetProperty("audio").GetArrayLength());
            };
            client.Failed += (_, message) =>
            {
                var failure = new IOException(message);
                hello.TrySetException(failure);
                inventory.TrySetException(failure);
            };
            try
            {
                report = Path.GetFullPath(report);
                Directory.CreateDirectory(Path.GetDirectoryName(report)!);
                var data = Environment.GetEnvironmentVariable("BLUEHEAD_DATA_DIR");
                if (string.IsNullOrWhiteSpace(data)) data = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-data");
                phase = "start_engine";
                await client.StartAsync(data!);
                phase = "hello";
                await hello.Task.WaitAsync(TimeSpan.FromSeconds(20));
                phase = "request_inventory";
                await client.SendAsync(new { command = "inventory" });
                phase = "inventory";
                var endpoints = await inventory.Task.WaitAsync(TimeSpan.FromSeconds(20));
                phase = "shutdown";
                await client.ShutdownAsync();
                phase = "complete";
                File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, phase, audio_endpoints = endpoints, protocol = 1, clean_shutdown = true, scope = (string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("BLUEHEAD_SOURCE_ROOT")) ? "Packaged" : "Source") + " desktop-engine handshake and readonly inventory" }));
                passed = true;
            }
            catch (Exception error) { File.WriteAllText(report, JsonSerializer.Serialize(new { passed = false, phase, error = error.ToString() })); }
            finally { app.Shutdown(); }
        });
        app.Run(); return passed ? 0 : 1;
    }
}
