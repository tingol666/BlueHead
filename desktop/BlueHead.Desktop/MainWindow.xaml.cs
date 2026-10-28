using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using BlueHead.Platform;
using Microsoft.Win32;
using Application = System.Windows.Application;
using Button = System.Windows.Controls.Button;
using MessageBox = System.Windows.MessageBox;

namespace BlueHead.Desktop;

public partial class MainWindow
{
    // Editors live in the reusable control's namescope; use explicit tags to resolve their content.
    private static T EditorPart<T>(object content, string tag) where T : FrameworkElement
    {
        static T? Find(object node, string key)
        {
            if (node is T element && Equals(element.Tag, key)) return element;
            if (node is DependencyObject parent)
                foreach (var child in LogicalTreeHelper.GetChildren(parent))
                    if (Find(child, key) is { } match) return match;
            return null;
        }
        return Find(content, tag) ?? throw new KeyNotFoundException(tag);
    }
    internal ComboBox OutputPicker => EditorPart<ComboBox>(PlaybackGain.EditorContent, "OutputPicker");
    internal ComboBox InputPicker => EditorPart<ComboBox>(MicrophoneGain.EditorContent, "InputPicker");
    internal ComboBox SystemPicker => EditorPart<ComboBox>(SystemGain.EditorContent, "SystemPicker");
    internal CheckBox MuteToggle => EditorPart<CheckBox>(MicrophoneGain.FooterContent, "MuteToggle");
    internal TextBlock TransmitHint => EditorPart<TextBlock>(MicrophoneGain.FooterContent, "TransmitHint");
    internal TextBlock SystemAudioStatus => EditorPart<TextBlock>(SystemGain.FooterContent, "SystemAudioStatus");
    internal readonly DesktopState State = new();
    private readonly EngineClient engine = new();
    private EngineSettings settings = new();
    private readonly Dictionary<string, string> audioHealthNotices = new();
    private UiSettings preferences = new();
    private bool applying = true, running, closing, allowClose, driverBusy, refreshingAudio, stopping, starting;
    private bool receptionRequested;
    private int helloGeneration = -1;
    private readonly bool offline;
    internal bool SelfTestExitLifecycle { get; set; }
    internal Func<Task>? SelfTestShutdown { get; set; }
    internal List<string> SelfTestExitErrors { get; } = [];
    internal Task? ExitTask { get; private set; }
    internal List<JsonElement> SelfTestCommands { get; } = [];
    private int requestId;
    private readonly Dictionary<string, int> pending = [];
    private readonly Dictionary<string, DeviceCard> devices = [];
    private JsonElement? telemetry;
    private readonly string dataDirectory;
    private readonly DispatcherTimer timer;
    private readonly DispatcherTimer gainTimer;
    private readonly Dictionary<string, object> queuedGains = [];
    private int? systemAudioRequest;
    private bool systemAudioActive;
    private bool systemFeedbackBlocked;
    private bool inventoryReceived;
    private int inventoryRevision;
    private List<DriverDevice> driverInventory = [];
    private bool driversInspected, systemRadioReady, driverChecking;
    private Task? driverRefreshTask;
    private string? driverInventoryError, preparedDriverInstance, pendingSetupMode, driverResultInstance;
    internal List<string> SelfTestExternalLinks { get; } = [];
    private bool mediaModePending, voiceInUse, mediaProtected, highMediaActive;
    private System.Windows.Forms.NotifyIcon? tray;
    internal MainWindow(bool offline = false)
    {
        this.offline = offline;
        var portableRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
        dataDirectory = Environment.GetEnvironmentVariable("BLUEHEAD_DATA_DIR") ??
            (File.Exists(Path.Combine(portableRoot, "portable.flag")) ? Path.Combine(portableRoot, "data") :
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "BlueHead", "data"));
        InitializeComponent(); DataContext = State; InitializeProtocolControls(); InitializeCodecControls();
        if (!offline) WindowSizing.Track(this);
        LocationChanged += (_, _) => CloseAudioEditors();
        SizeChanged += (_, _) => { CloseAudioEditors(); UpdateAudioFooterLayout(); };
        Deactivated += (_, _) => CloseAudioEditors();
        ShowPage("home");
        timer = new() { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += async (_, _) => { if (engine.Ready && running && !stopping) await Send(new { command = "telemetry" }); };
        gainTimer = new() { Interval = TimeSpan.FromMilliseconds(120) };
        gainTimer.Tick += async (_, _) => await FlushGains();
        engine.Message += (generation, message) => Dispatcher.BeginInvoke(() => ApplyEngineMessage(generation, message));
        engine.Failed += (generation, message) => Dispatcher.BeginInvoke(() => ApplyEngineFailure(generation, message));
        Closing += OnClosing;
        Loaded += async (_, _) =>
        {
            if (offline) { applying = false; StartButton.IsEnabled = true; return; }
            LoadPreferences(); SetupTray();
            try { await EnsureBackend(); timer.Start(); }
            catch (Exception error) { ApplyEngineFailure(engine.Generation, error.Message); }
        };
    }
    private void LoadPreferences()
    {
        try { preferences = JsonSerializer.Deserialize<UiSettings>(File.ReadAllText(Path.Combine(dataDirectory, "desktop.json"))) ?? new(); }
        catch (Exception error) when (error is IOException or JsonException) { }
        TrayToggle.IsChecked = preferences.Tray; AutoStartToggle.IsChecked = preferences.AutoStart;
        if (!string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("BLUEHEAD_SOURCE_ROOT")))
        { AutoStartToggle.IsEnabled = false; AutoStartToggle.ToolTip = "源码运行不修改 Windows 自启动。请在正式安装的程序中设置。"; }
    }
    private void SetupTray()
    {
        var icon = Path.Combine(AppContext.BaseDirectory, "bluehead.ico");
        tray = new() { Text = "BlueHead", Icon = File.Exists(icon) ? new System.Drawing.Icon(icon) : System.Drawing.SystemIcons.Application, Visible = true };
        var menu = new System.Windows.Forms.ContextMenuStrip();
        menu.Items.Add("打开 BlueHead", null, (_, _) => Dispatcher.Invoke(ShowFromTray));
        menu.Items.Add("恢复系统蓝牙…", null, (_, _) => Dispatcher.Invoke(() => { ShowFromTray(); ShowPage("adapters"); }));
        menu.Items.Add("退出", null, (_, _) => Dispatcher.Invoke(() => { allowClose = true; Close(); }));
        tray.ContextMenuStrip = menu; tray.MouseClick += TrayMouseClick;
    }
    internal void TrayMouseClick(object? sender, System.Windows.Forms.MouseEventArgs e)
    {
        if (e.Button == System.Windows.Forms.MouseButtons.Left) Dispatcher.Invoke(ShowFromTray);
    }
    internal void ShowFromTray() { if (closing) return; Show(); WindowState = WindowState.Normal; Activate(); }
    internal void ShowPage(string page)
    {
        CloseAudioEditors();
        if (page != "adapters" && HasPendingMode) { CancelModeChoice(); UpdateDriverActions(); }
        foreach (var (button, name) in new[] { (NavHome, "home"), (NavBroadcast, "broadcast"), (NavSettings, "settings"), (NavAdapters, "adapters"), (NavHelp, "help") })
        {
            button.Background = new System.Windows.Media.SolidColorBrush(name == page ? System.Windows.Media.Color.FromRgb(225, 246, 236) : System.Windows.Media.Colors.Transparent);
            button.Foreground = new System.Windows.Media.SolidColorBrush(name == page ? System.Windows.Media.Color.FromRgb(7, 139, 78) : System.Windows.Media.Color.FromRgb(100, 119, 134));
        }
        HomePage.Visibility = page == "home" ? Visibility.Visible : Visibility.Collapsed;
        BroadcastPage.Visibility = page == "broadcast" ? Visibility.Visible : Visibility.Collapsed;
        SettingsPage.Visibility = page == "settings" ? Visibility.Visible : Visibility.Collapsed;
        AdaptersPage.Visibility = page == "adapters" ? Visibility.Visible : Visibility.Collapsed;
        if (page == "adapters") AdaptersScroll.ScrollToTop();
        HelpPage.Visibility = page == "help" ? Visibility.Visible : Visibility.Collapsed;
        if (page is "broadcast" or "settings") UpdateProtocolAvailability();
        if (controllerNotice) Notice.Visibility = page == "adapters" ? Visibility.Collapsed : Visibility.Visible;
        NoticeAction.Visibility = CurrentControllerGuide != null && Notice.IsVisible && page != "adapters" ? Visibility.Visible : Visibility.Collapsed;
    }
    private void HomeClick(object sender, RoutedEventArgs e) => ShowPage("home");
    private void SettingsClick(object sender, RoutedEventArgs e) => ShowPage("settings");
    private async void AdaptersClick(object sender, RoutedEventArgs e) { ShowPage("adapters"); await RefreshDrivers(); }
    private void HelpClick(object sender, RoutedEventArgs e) => ShowPage("help");
    private void PairingClick(object sender, RoutedEventArgs e) => ShowPage("help");
    private void ShowNotice(string text) { protocolCapabilityNotice = false; controllerNotice = false; NoticeText.Text = text; Notice.Visibility = Visibility.Visible; }
    private async Task EnsureBackend()
    {
        if (offline) return;
        await engine.StartAsync(dataDirectory);
        if (engine.Hello is { } hello && helloGeneration != engine.Generation) ApplyEngineMessage(engine.Generation, hello);
        if (!timer.IsEnabled) timer.Start();
    }
    internal void ApplyEngineMessage(int generation, JsonElement message)
    {
        // Dispatcher work queued by a retired session cannot resurrect its
        // connections or overwrite a replacement session's settings.
        if (closing || generation != engine.Generation || (!offline && !engine.Ready)) return;
        if (message.Text("event") == "hello")
        {
            if (helloGeneration == generation) return;
            helloGeneration = generation;
        }
        ApplyEvent(message);
    }
    private void ClearSession()
    {
        EndCodecConnections();
        VoiceProtectionNotice.Visibility = Visibility.Collapsed; VoiceProtectionText.Text = "";
        voiceInUse = mediaProtected = highMediaActive = systemAudioActive = systemFeedbackBlocked = refreshingAudio = false;
        sessionMediaOnly = mediaModePending = false; systemAudioRequest = null;
        pending.Clear(); queuedGains.Clear(); gainTimer.Stop(); audioHealthNotices.Clear();
        State.Devices.Clear(); devices.Clear(); telemetry = null;
        DiagnosticsText.Text = "当前没有有效接收会话。";
        UpdateDevices(); UpdateMixState(); UpdateMediaControls();
    }
    internal void ApplyEngineFailure(int generation, string message)
    {
        if (closing || generation != engine.Generation) return;
        stopping = false; ClearSession(); SetRunning(false);
        EngineState.Text = "后台已断开"; FooterStatus.Text = "连接已清除 · 点击开始接收恢复";
        ShowNotice(message);
    }
    private void BeginStopping()
    {
        stopping = true; ClearSession(); SetRunning(true);
        StartButton.Content = "正在停止…"; StartButton.IsEnabled = false;
        DriverStopButton.IsEnabled = false;
        StatusTitle.Text = "正在停止接收"; StatusDetail.Text = "正在释放蓝牙与音频设备，请稍候。";
    }
    private async Task<bool> Send(object message)
    {
        if (offline) { SelfTestCommands.Add(JsonSerializer.SerializeToElement(message)); return true; }
        try { await engine.SendAsync(message); return true; }
        catch (Exception error) when (error is IOException or InvalidOperationException or ObjectDisposedException)
        { if (!engine.Ready) ApplyEngineFailure(engine.Generation, error.Message); else ShowNotice(error.Message); return false; }
    }
    private EngineSettings ReadSettings()
    {
        settings.name = NameEntry.Text.Trim(); settings.calls = CallsToggle.IsChecked == true;
        settings.voice_single_device = VoiceProtectionToggle.IsChecked == true;
        settings.codec = (CodecPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
        settings.transport = (AdapterPicker.SelectedItem as AdapterChoice)?.Transport ?? settings.transport;
        settings.buffer_ms = int.Parse((MediaBufferPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "60");
        settings.call_buffer_ms = int.Parse((CallBufferPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "30");
        settings.maximum_devices = int.Parse((MaximumPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "3");
        settings.ldac_enabled = LdacToggle.IsChecked == true;
        settings.lhdc5_enabled = Lhdc5Toggle.IsChecked == true;
        settings.le_audio_enabled = LeAudioToggle.IsChecked == true;
        settings.connection_policy = settings.le_audio_enabled ? "prefer_le" : "classic";
        return settings;
    }
    private void ApplySettings()
    {
        applying = true;
        NameEntry.Text = settings.name; CallsToggle.IsChecked = settings.calls; LdacToggle.IsChecked = settings.ldac_enabled;
        VoiceProtectionToggle.IsChecked = settings.voice_single_device;
        Lhdc5Toggle.IsChecked = settings.lhdc5_enabled; LeAudioToggle.IsChecked = settings.le_audio_enabled;
        SelectTag(ConnectionPolicyPicker, settings.le_audio_enabled ? "prefer_le" : "classic");
        SelectTag(CodecPicker, settings.codec); RefreshCodecChoices(); SelectTag(PlaybackModePicker, settings.media_mode); SelectTag(ModePicker, settings.mode); SelectTag(HomeModePicker, settings.mode);
        SelectTag(MediaBufferPicker, settings.buffer_ms.ToString()); SelectTag(CallBufferPicker, settings.call_buffer_ms.ToString());
        SelectTag(MaximumPicker, settings.maximum_devices.ToString());
        MuteToggle.IsChecked = settings.microphone_muted;
        PlaybackGain.Value = settings.playback_gain * 100;
        MicrophoneGain.Value = settings.microphone_gain * 100;
        SystemGain.Value = settings.system_gain * 100;
        SystemAudioToggle.IsChecked = settings.system_audio_enabled;
        OutputPicker.SelectedItem = State.Outputs.FirstOrDefault(item => item.Index == settings.output_device) ?? State.Outputs.FirstOrDefault();
        InputPicker.SelectedItem = State.Inputs.FirstOrDefault(item => item.Index == settings.input_device) ?? State.Inputs.FirstOrDefault();
        SystemPicker.SelectedItem = State.SystemSources.FirstOrDefault(item => item.EndpointId == settings.system_endpoint_id) ?? State.SystemSources.FirstOrDefault();
        applying = false;
        SetPickerStates();
        foreach (var card in State.Devices) card.CanMix = settings.mode != "native";
        PairingHint.Text = settings.mode == "native" ? "在电脑的 Windows 蓝牙设置中添加并配对手机／平板；回 BlueHead 点击“开始接收”，然后在手机／平板播放声音。" : $"点击右上角“开始接收”，然后在手机／平板的蓝牙列表连接“{settings.name}”。";
    }
    private static void SelectTag(ComboBox picker, string tag) => picker.SelectedItem = picker.Items.OfType<ComboBoxItem>().FirstOrDefault(item => item.Tag?.ToString() == tag);
    internal void ApplyEvent(JsonElement message)
    {
        var kind = message.Text("event");
        if (stopping && kind is not ("stopped" or "stopping" or "error" or "warning")) return;
        if (ApplyCodecMessage(message)) return;
        ApplyProtocolMessage(message);
        if (kind == "hello")
        {
            settings = JsonSerializer.Deserialize<EngineSettings>(message.GetProperty("settings")) ?? new();
            ApplySettings(); EngineState.Text = "后台已就绪"; StartButton.IsEnabled = true;
            _ = Send(new { command = "inventory" }); _ = RefreshDrivers();
        }
        else if (kind == "inventory")
        {
            inventoryReceived = true;
            applying = true;
            var adapters = message.GetProperty("adapters").EnumerateArray().Select(item => LabelAdapter(new AdapterChoice(item.Text("transport"), item.Text("name")) { Vid = (int)item.Number("vid"), Pid = (int)item.Number("pid"), VoiceTransport = item.Flag("voice_transport") })).ToList();
            AdapterPicker.ItemsSource = adapters;
            var prepared = driverInventory.FirstOrDefault(device => device.InstanceId == preparedDriverInstance);
            var preparedChoices = prepared == null ? Array.Empty<AdapterChoice>() : adapters.Where(item => Drivers.MatchTransport(item.Transport, new[] { prepared }).Count == 1).ToArray();
            var selectedTransport = Drivers.SelectTransport(settings.transport, adapters.Select(item => (item.Transport, item.Vid, item.Pid)));
            AdapterPicker.SelectedItem = preparedChoices.Length == 1 && settings.mode != "native" ? preparedChoices[0] : adapters.FirstOrDefault(item => item.Transport == selectedTransport);
            if (AdapterPicker.SelectedItem is AdapterChoice actual) settings.transport = actual.Transport;
            if (preparedChoices.Length == 1 && settings.mode != "native") { settings.transport = preparedChoices[0].Transport; preparedDriverInstance = null; }
            State.Outputs.Clear(); State.Inputs.Clear(); State.SystemSources.Clear();
            State.SystemSources.Add(new(null, "全部应用声音（排除 BlueHead）"));
            State.Outputs.Add(new(null, "默认播放设备（自动跟随）")); State.Inputs.Add(new(null, "默认麦克风（自动跟随）"));
            foreach (var item in message.GetProperty("audio").EnumerateArray())
            {
                var choice = new AudioChoice(item.GetProperty("index").GetInt32(), item.Text("name"), item.Text("endpoint_id"));
                if (item.GetProperty("max_output_channels").GetInt32() >= 2) { State.Outputs.Add(choice); State.SystemSources.Add(choice); }
                if (item.GetProperty("max_input_channels").GetInt32() >= 1) State.Inputs.Add(choice);
            }
            if (settings.output_device != null && !State.Outputs.Any(item => item.Index == settings.output_device))
            {
                if (running) State.Outputs.Add(new(settings.output_device, "原播放设备（未列出）"));
                else settings.output_device = null;
            }
            if (settings.input_device != null && !State.Inputs.Any(item => item.Index == settings.input_device))
            {
                if (running) State.Inputs.Add(new(settings.input_device, "原麦克风（未列出）"));
                else settings.input_device = null;
            }
            if (settings.system_endpoint_id != null && !State.SystemSources.Any(item => item.EndpointId == settings.system_endpoint_id))
                State.SystemSources.Add(new(settings.system_device, "原电脑声音设备（未连接）", settings.system_endpoint_id));
            ApplySettings();
            UpdateSetupState(); inventoryRevision++;
        }
        else if (kind == "starting") { stopping = false; sessionMediaOnly = false; audioHealthNotices.Clear(); Notice.Visibility = Visibility.Collapsed; SetRunning(true); StatusTitle.Text = "正在准备接收"; StatusDetail.Text = "正在打开蓝牙与音频设备…"; }
        else if (kind == "stopping") BeginStopping();
        else if (kind == "stopped")
        {
            stopping = false; ClearSession(); SetRunning(false);
            // A controller fault is presented for explicit recovery; never raise UAC here.
        }
        else if (kind == "controller_capabilities")
        {
            sessionMediaOnly = !message.Flag("voice_transport"); ClearControllerFailure(); SetPickerStates();
            if (sessionMediaOnly) ShowProtocolCapability(message.Flag("le_audio_pending") ? "此适配器不提供经典蓝牙语音，正在检查 LE Audio 的电脑麦克风能力…" : MediaOnlyNotice);
        }
        else if (kind is "error" or "warning") { if (!ApplyControllerFailure(message)) ShowNotice(message.Text("message")); }
        else if (kind == "voice_protection")
        {
            VoiceProtectionText.Text = message.Text("message");
            VoiceProtectionNotice.Visibility = message.Text("message").Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        }
        else if (kind == "audio_health")
        {
            var direction = message.Text("direction");
            if (message.Text("state") == "restored")
            {
                if (audioHealthNotices.TryGetValue(direction, out var previous) && NoticeText.Text == previous)
                    Notice.Visibility = Visibility.Collapsed;
                audioHealthNotices.Remove(direction);
                FooterStatus.Text = message.Text("message");
                if (Notice.Visibility != Visibility.Visible && audioHealthNotices.Count > 0)
                    ShowNotice(audioHealthNotices.Values.Last());
            }
            else
            {
                audioHealthNotices[direction] = message.Text("message");
                ShowNotice(message.Text("message"));
            }
        }
        else if (kind == "status")
        {
            StatusDetail.Text = message.Text("message");
            if (StatusDetail.Text.StartsWith("已就绪")) { StatusTitle.Text = "正在接收"; Notice.Visibility = Visibility.Collapsed; }
        }
        else if (kind == "audio_refreshing") { refreshingAudio = true; StartButton.IsEnabled = false; SetPickerStates(); FooterStatus.Text = "正在更新音频设备…"; }
        else if (kind == "audio_refreshed")
        {
            refreshingAudio = false; StartButton.IsEnabled = !driverBusy && !closing;
            settings.output_device = message.Endpoint("output_device"); settings.input_device = message.Endpoint("input_device");
            if (message.TryGetProperty("output_endpoint_id", out var outputIdentity))
                settings.output_endpoint_id = outputIdentity.ValueKind == JsonValueKind.String ? outputIdentity.GetString() : null;
            else if (settings.output_device == null) settings.output_endpoint_id = null;
            if (message.TryGetProperty("input_endpoint_id", out var inputIdentity))
                settings.input_endpoint_id = inputIdentity.ValueKind == JsonValueKind.String ? inputIdentity.GetString() : null;
            else if (settings.input_device == null) settings.input_endpoint_id = null;
            FooterStatus.Text = message.GetProperty("success").GetBoolean() ? "音频设备已更新 · 蓝牙连接保持" : "音频设备更新未完成";
            if (!message.GetProperty("success").GetBoolean()) ShowNotice(message.Text("message"));
            ApplySettings();
        }
        else if (kind == "audio_device")
        {
            var direction = message.Text("direction");
            if (!pending.TryGetValue(direction, out var id) || id != message.GetProperty("request_id").GetInt32()) return;
            pending.Remove(direction);
            if (message.GetProperty("success").GetBoolean())
            {
                if (direction == "output") { settings.output_device = message.Endpoint("device"); settings.output_endpoint_id = message.Text("endpoint_id", "") is { Length: > 0 } o ? o : null; }
                else if (direction == "system") { settings.system_device = message.Endpoint("device"); settings.system_endpoint_id = message.Text("endpoint_id", "") is { Length: > 0 } s ? s : null; }
                else { settings.input_device = message.Endpoint("device"); settings.input_endpoint_id = message.Text("endpoint_id", "") is { Length: > 0 } i ? i : null; }
                FooterStatus.Text = direction == "output" ? "播放设备已切换 · 蓝牙连接保持" : direction == "system" ? "电脑声音采集来源已切换 · 蓝牙连接保持" : "麦克风已切换 · 蓝牙连接保持";
            }
            else ShowNotice(message.Text("message"));
            ApplySettings();
        }
        else if (kind == "media_owner")
        {
            foreach (var card in State.Devices)
            {
                card.IsMediaSelected = card.Key == message.Text("key");
                card.MediaAction = card.IsMediaSelected ? "当前播放" : "切换播放";
            }
        }
        else if (kind == "media_mode")
        {
            mediaModePending = false;
            settings.media_mode = message.Text("mode", settings.media_mode);
            if (!message.Flag("success")) ShowNotice(message.Text("message"));
            ApplySettings(); UpdateMediaControls();
        }
        else if (kind == "configured") { SettingsHint.Text = "设置已保存，下次开始接收时使用。"; Notice.Visibility = Visibility.Collapsed; }
        else if (kind == "system_audio")
        {
            if (systemAudioRequest != message.Endpoint("request_id")) return;
            systemAudioRequest = null;
            settings.system_audio_enabled = message.Flag("enabled");
            if (!message.Flag("success")) ShowNotice(message.Text("message"));
            if (!settings.system_audio_enabled) systemAudioActive = false;
            ApplySettings();
        }
        else if (kind == "system_audio_state")
        {
            systemAudioActive = message.Flag("active");
            UpdateMixState();
            if (!systemAudioActive) ShowNotice(message.Text("message"));
        }
        else if (kind == "telemetry")
        {
            if (!running) return;
            if (message.TryGetProperty("running", out var receiverRunning) && !receiverRunning.GetBoolean())
            { ClearSession(); SetRunning(false); return; }
            telemetry = message.Clone();
            var audio = message.GetProperty("audio");
            DiagnosticsText.Text = audio.TryGetProperty("native_mixer", out var native)
                ? $"音频引擎：{(native.GetBoolean() ? "原生 PCM 混音" : "兼容混音")}\n媒体缓冲：{audio.GetProperty("media_buffer_ms")} ms\n语音缓冲：{audio.GetProperty("call_buffer_ms")} ms\n缓冲不足：{audio.GetProperty("underruns")}\n回调竞争：{audio.GetProperty("callback_contentions")}"
                : "后台已就绪，接收尚未启动。";
            systemFeedbackBlocked = systemAudioActive = voiceInUse = mediaProtected = highMediaActive = false;
            if (message.TryGetProperty("devices", out var streams))
            {
                var connected = new HashSet<string>();
                foreach (var stream in streams.EnumerateArray())
                {
                    var key = stream.Text("key");
                    if (key.Length == 0) continue;
                    connected.Add(key);
                    if (!devices.TryGetValue(key, out var card))
                    {
                        var gains = settings.device_volumes.GetValueOrDefault(key) ?? new();
                        card = new() { Key = key, Connection = "已连接", CanMix = settings.mode != "native", Volume = gains.down * 100, UplinkVolume = gains.up * 100 };
                        devices.Add(key, card); State.Devices.Add(card);
                    }
                    ApplyLiveAudio(card, stream);
                    if (stream.TryGetProperty("media", out var media)) { mediaProtected |= media.Flag("protected"); highMediaActive |= media.Text("codec") is "aptx_hd" or "aptx" or "ldac" or "lhdc5"; }
                    if (stream.TryGetProperty("call", out var voice)) voiceInUse |= voice.Flag("active");
                    if (stream.TryGetProperty("call", out var call) && call.Flag("system_audio_active")) systemAudioActive = true;
                    if (stream.TryGetProperty("call", out var feedback)) systemFeedbackBlocked |= feedback.Flag("system_feedback_blocked");
                }
                // A full snapshot is authoritative. Heal a missed/late profile
                // event instead of keeping a disconnected card indefinitely.
                var complete = message.TryGetProperty("devices_complete", out var completeValue) ? completeValue.GetBoolean() : settings.mode != "native";
                if (complete)
                    foreach (var key in devices.Keys.Where(key => !connected.Contains(key)).ToArray())
                    { State.Devices.Remove(devices[key]); devices.Remove(key); }
                UpdateDevices();
            }
            UpdateMixState(); UpdateMediaControls();
        }
        else if (message.TryGetProperty("key", out var keyElement))
        {
            var key = keyElement.GetString() ?? "";
            if (!devices.TryGetValue(key, out var card))
            {
                // Only a connection event creates a card. Late call/name/mic
                // events belong to a disconnected generation and are ignored.
                if (!running || kind != "device" || message.Text("state") == "已断开") return;
                var gains = settings.device_volumes.GetValueOrDefault(key) ?? new();
                card = new() { Key = key, CanMix = settings.mode != "native", Volume = gains.down * 100, UplinkVolume = gains.up * 100 };
                if (settings.mode == "native") { card.Name = key; card.Media = "Windows 系统播放 · 媒体编码与码率由系统管理"; }
                devices.Add(key, card); State.Devices.Add(card);
            }
            switch (kind)
            {
                case "name": card.Name = message.Text("name", "移动设备"); break;
                case "format": card.Media = $"{message.Text("protocol", "A2DP")} / {CodecName(message.Text("codec"))} · {message.Number("rate") / 1000:0.###} kHz · — kb/s"; break;
                case "device":
                    card.Connection = message.Text("state");
                    card.LiveDetails = card.Connection.StartsWith("已连接") ? "已连接 · " + VoiceCodecStatus : card.Connection;
                    if (card.Connection == "已断开") { State.Devices.Remove(card); devices.Remove(key); }
                    break;
                case "call":
                    card.CallDescription = message.Text("state");
                    var phase = message.Text("phase");
                    card.CanAnswer = phase == "incoming" || card.CallDescription == "来电";
                    card.CanHangup = phase is "incoming" or "active" or "outgoing";
                    card.LiveDetails = card.CallDescription + "\n" + VoiceCodecStatus;
                    break;
                case "microphone": if (message.Flag("active")) card.LiveDetails = "语音通道已连接 · 麦克风已启用\n" + VoiceCodecStatus; break;
                case "avrcp": card.ApplyRemoteMedia(message); break;
            }
            UpdateDevices();
        }
    }
    private void UpdateDevices()
    {
        DeviceCount.Text = $"{State.Devices.Count} 台设备";
        foreach (var item in codecWindows) if (!devices.ContainsKey(item.Key)) item.Value.Disconnected();
        UpdateSetupState(); UpdateMediaControls(); RefreshOpenHelp();
    }
    private DriverDevice? ReceptionAdapter
    {
        get
        {
            var transport = (AdapterPicker.SelectedItem as AdapterChoice)?.Transport ?? settings.transport;
            var matches = Drivers.MatchTransport(transport, driverInventory);
            return matches.Count == 1 ? matches[0] : AdapterPicker.SelectedItem == null ? DriverPicker.SelectedItem as DriverDevice : null;
        }
    }
    internal ReceptionGuide ReceptionGuide
    {
        get
        {
            var guide = BluetoothSetup.Reception(settings.mode == "native", driversInspected,
                driverInventoryError != null, AdapterPicker.SelectedItem is AdapterChoice, ReceptionAdapter,
                systemRadioReady, driverInventory.Any(device => BluetoothSetup.Dedicated(device) && device.HasBackup));
            return guide.Ready && CurrentControllerGuide is { } preparation ? new(BluetoothSetupStage.InitializationRequired, preparation.Title, preparation.Detail,
                ControllerNeedsInitialization ? "处理蓝牙问题" : "检查蓝牙启动") : guide;
        }
    }
    internal bool AdapterNeedsRestart => ReceptionGuide.Stage == BluetoothSetupStage.RestartRequired;
    internal bool NeedsBluetoothSetup => !ReceptionGuide.Ready;
    private void UpdateModePresentation(ReceptionGuide guide)
    {
        var shared = settings.mode == "native";
        SharedModeNotice.Visibility = Visibility.Collapsed;
        HomePairButton.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        SharedSettingsNotice.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        DedicatedAudioFooter.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedAudioFooter.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        DedicatedNameFields.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedNameFields.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        AdvancedAudio.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        VoiceSettingsRow.Visibility = VoiceProtectionRow.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        VoiceProtectionToggle.IsEnabled = !running && !stopping && !starting && !driverBusy;
        SharedCodecValue.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        CodecPicker.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        CodecSettingsButton.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        PlaybackModePicker.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedPlaybackValue.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        ModeStatusButton.Content = "驱动设置";
        ModeStatusButton.ToolTip = "查看蓝牙归谁管理、切换或恢复驱动";
        HomeModeState.Text = (running ? "使用中 · " : guide.Ready ? "已就绪 · " : "待准备 · ") + ModeName(settings.mode);
        HomeModeSummary.Text = shared ? "只接收音乐／视频，保留 Windows 蓝牙设备。专用方式还支持电脑麦克风、媒体编码选择和多设备同时连接。" : "支持电脑麦克风、媒体编码选择、多设备同时连接和播放。";
        HomeNextStep.Text = running ? shared ? "在手机／平板播放音乐或视频。声音大小与播放耳机在 Windows 中调整。" : $"在手机／平板的蓝牙列表连接“{settings.name}”，然后播放声音；需要麦克风时，在对方应用中选择蓝牙音频。" : shared ? "① 电脑上打开 Windows 蓝牙设置，添加并配对手机／平板（已配对可跳过）。\n② 回到 BlueHead 点击右上角“开始接收”。③ 在手机／平板播放音乐或视频。" : guide.Ready ? $"① 点击右上角“开始接收”。② 在手机／平板的蓝牙列表连接“{settings.name}”，然后播放声音。" : "先点击下方“打开蓝牙驱动设置”完成准备，再回音频中心点击右上角“开始接收”。";
        PairingHint.Text = shared ? "在手机／平板播放音乐或视频；若没有声音，在该设备的音频输出菜单中选电脑。" : $"在手机／平板的蓝牙列表连接“{settings.name}”，然后播放音乐或视频。";
        foreach (var card in State.Devices) card.CanMix = !shared;
        HelpSteps.Text = shared ? "基础模式 · Windows 共用蓝牙\n\n1. 在电脑上点击“打开 Windows 蓝牙”，打开蓝牙，点击“添加设备”→“蓝牙”，选择手机／平板。按电脑与手机／平板的提示确认配对。已配对的设备可跳过此步。\n\n2. 回到 BlueHead 的“音频中心”，点击右上角“开始接收”。BlueHead 会打开已配对设备的声音连接，无需再找一个“开始连接”按钮。\n\n3. 在手机／平板播放音乐或视频。若没有声音，在手机／平板的音频输出菜单选择电脑的蓝牙名称。播放到哪个耳机、声音大小在 Windows 中调整。\n\n需要把电脑麦克风发给设备？在音频中心改选进阶模式，按蓝牙驱动设置页提示完成驱动切换。" : $"进阶模式 · 专用蓝牙\n\n1. 在“蓝牙驱动设置”选择电脑的蓝牙设备与进阶模式。若提示由 Windows 管理，先读清影响，在页面底部右侧点击“备份并启用专用蓝牙”，允许 Windows 的管理员授权。\n\n2. 返回“音频中心”，点击右上角“开始接收”。在手机／平板的蓝牙设置中查找“{settings.name}”，点击它并确认配对。\n\n3. 在手机／平板播放音乐或视频。需要电脑麦克风时，打开该设备蓝牙连接的“通话音频”选项，并在通话、录音或录像应用中选择蓝牙音频。";
        UpdateModeChoice();

    }
    private void UpdateSetupState()
    {
        UpdateDriverActions();
        var guide = ReceptionGuide;
        UpdateModePresentation(guide);
        if (!inventoryReceived) return;
        var setup = !running && !guide.Ready && guide.Stage != BluetoothSetupStage.Checking;
        SetupCard.Visibility = setup ? Visibility.Visible : Visibility.Collapsed;
        MediaProfileCard.Visibility = setup || settings.mode == "native" ? Visibility.Collapsed : Visibility.Visible;
        EmptyState.Visibility = State.Devices.Count == 0 && !setup && guide.Stage != BluetoothSetupStage.Checking ? Visibility.Visible : Visibility.Collapsed;
        StartButton.Content = running ? "停止接收" : guide.Action;
        StartButton.IsEnabled = !driverBusy && !driverChecking && !closing && (running || !refreshingAudio && guide.Stage != BluetoothSetupStage.Checking);
        SetupTitle.Text = guide.Title;
        SetupDescription.Text = driverInventoryError ?? guide.Detail;
        SetupAction.Content = "打开蓝牙驱动设置";
        SetupBenefits.Visibility = Visibility.Collapsed;
        SetupCaption.Text = "";
        FinishBluetoothButton.Content = "返回音频中心";
        FinishBluetoothButton.IsEnabled = !stopping && !starting && !driverBusy && !driverChecking && !closing;
        FinishBluetoothHint.Text = HasPendingMode ? "本次模式选择尚未生效，返回将取消这次选择。" : "这里的操作不会自动开始接收。";
        RefreshBluetoothButton.IsEnabled = !driverBusy && !driverChecking;
        if (controllerNotice) Notice.Visibility = AdaptersPage.IsVisible ? Visibility.Collapsed : Visibility.Visible;
        NoticeAction.Visibility = CurrentControllerGuide != null && Notice.IsVisible && !AdaptersPage.IsVisible ? Visibility.Visible : Visibility.Collapsed;
        DriverStopButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        if (running) return;
        StatusTitle.Text = guide.Ready ? "未接收" : guide.Stage == BluetoothSetupStage.RestartRequired ? "等待重启" : "待设置";
        StatusDetail.Text = guide.Detail;
    }
    internal IEnumerable<AudioLevel> AudioEditors()
    {
        static IEnumerable<AudioLevel> Find(DependencyObject parent)
        {
            for (var i = 0; i < System.Windows.Media.VisualTreeHelper.GetChildrenCount(parent); i++)
            {
                var child = System.Windows.Media.VisualTreeHelper.GetChild(parent, i);
                if (child is AudioLevel level) yield return level;
                else foreach (var nested in Find(child)) yield return nested;
            }
        }
        return Find(this);
    }
    internal void CloseAudioEditors(AudioLevel? except = null, DeviceCard? detailsExcept = null)
    {
        if (AudioOptionHelpPopup != null) AudioOptionHelpPopup.IsOpen = false;
        foreach (var editor in AudioEditors()) if (editor != except) editor.SetCurrentValue(AudioLevel.IsOpenProperty, false);
        foreach (var card in State.Devices) if (card != detailsExcept) card.DetailsOpen = false;
    }
    private void DeviceDetailsOpened(object? sender, EventArgs e)
    {
        if (sender is System.Windows.Controls.Primitives.Popup { Tag: DeviceCard card }) CloseAudioEditors(detailsExcept: card);
    }
    private async void SetupBluetoothClick(object sender, RoutedEventArgs e)
    {
        if (driverBusy || closing || stopping) return;
        if (ReferenceEquals(sender, SetupAction) || ReferenceEquals(sender, StartButton))
        {
            if (ReceptionGuide.Stage == BluetoothSetupStage.SystemBluetoothRequired) { OpenSettings("ms-settings:bluetooth"); return; }
            if (ReceptionGuide.Stage == BluetoothSetupStage.CheckFailed) { await RefreshDrivers(); await Send(new { command = "inventory" }); return; }
        }
        ShowPage("adapters"); await RefreshDrivers();
        if (ReceptionGuide.Stage == BluetoothSetupStage.InitializationRequired)
        {
            DriverPicker.SelectedItem = driverInventory.FirstOrDefault(device => device.AdapterId == controllerFailureId);
            UpdateControllerActions(); UpdateSetupState();
        }
    }

    private void OpenSettings(string uri)
    {
        if (offline) { SelfTestExternalLinks.Add(uri); return; }
        try { Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true }); }
        catch (Exception error) { ShowNotice("无法打开 Windows 设置：" + error.Message); }
    }
    private void WindowsBluetoothClick(object sender, RoutedEventArgs e) => OpenSettings("ms-settings:bluetooth");
    private void WindowsSoundClick(object sender, RoutedEventArgs e) => OpenSettings("ms-settings:sound");
    private async void DedicatedSetupClick(object sender, RoutedEventArgs e)
    {
        ShowPage("adapters");
        if (running) { pendingSetupMode = "advanced"; DriverResult.Text = "请先点击“先停止接收”，然后选择进阶模式。"; return; }
        await RefreshDrivers(); await ChooseMode("advanced", false);
    }
    private async void StopForSetupClick(object sender, RoutedEventArgs e)
    {
        if (!running || driverBusy || stopping) return;
        receptionRequested = false; BeginStopping(); await Send(new { command = "stop" });
    }
    private void SetIntendedMode(string mode)
    {
        if (settings.mode != mode)
        {
            CloseAudioEditors(); FooterStatus.Text = "本机蓝牙 · 无需同一 Wi-Fi";
            driverResultInstance = null; DriverResult.Text = "";
            Notice.Visibility = Visibility.Collapsed; audioHealthNotices.Clear();
        }
        settings.mode = mode; CancelModeChoice();
        SetPickerStates(); UpdateSetupState();
    }
    private async void FinishBluetoothClick(object sender, RoutedEventArgs e)
    {
        if (stopping || starting || driverBusy || closing) return;
        CancelModeChoice();
        if (running || await SaveSettings()) ShowPage("home");
    }
    private static string CodecName(string codec) => codec.ToLowerInvariant() switch
    { "aptx" => "aptX", "aptx_hd" => "aptX HD", "msbc" => "mSBC", "cvsd" => "CVSD", "lc3_swb" or "lc3-swb" => "LC3-SWB", "lhdc5" => "LHDC 5.0", "lc3" => "LC3", _ => codec.ToUpperInvariant() };
    private static string Bitrate(double bits) => bits >= 1000000 ? $"{bits / 1000000:0.##} Mb/s" : $"{bits / 1000:0.#} kb/s";
    private string VoiceCodecStatus => settings.mode == "native" ? "Windows 共用方式不提供蓝牙语音；电脑麦克风需改用 BlueHead 专用蓝牙"
        : settings.calls ? "语音编码：自动协商" : "语音编码：自动协商（语音与麦克风已关闭）";
    private static string DetailMetric(JsonElement value, string name, string prefix, string suffix, string format = "0.#")
    {
        if (!value.TryGetProperty(name, out var item) || item.ValueKind != JsonValueKind.Number ||
            !item.TryGetDouble(out var number) || double.IsNaN(number) || double.IsInfinity(number)) return "";
        return " · " + prefix + number.ToString(format) + suffix;
    }
    private void ApplyLiveAudio(DeviceCard card, JsonElement stream)
    {
        card.PlaybackNotice = "";
        if (stream.TryGetProperty("media", out var selection))
        {
            card.IsMediaSelected = settings.media_mode == "single" && selection.Flag("selected");
            card.MediaAction = card.IsMediaSelected ? "当前播放" : "切换播放";
        }
        if (stream.TryGetProperty("call", out var call) && call.Flag("active"))
        {
            card.Media = $"{call.Text("protocol", "HFP")} / {CodecName(call.Text("codec"))} · {call.Number("rate") / 1000:0.###} kHz · ↓ {Bitrate(call.Number("rx_bitrate"))}  ↑ {Bitrate(call.Number("tx_bitrate"))}";
            card.LiveDetails = $"{VoiceCodecStatus} · 当前 {CodecName(call.Text("codec"))} · {call.Number("rate") / 1000:0.###} kHz\n语音通道使用中" + DetailMetric(call, "rx_packets", "接收 ", " 包", "0") + DetailMetric(call, "tx_packets", "发送 ", " 包", "0") + $" · 麦克风{(settings.microphone_muted ? "已在电脑暂停发送" : call.Flag("microphone_remote_muted") ? "已被设备静音" : call.Flag("microphone_active") ? "工作中" : "未就绪")} · 电脑声音{(call.Flag("system_feedback_blocked") ? "已暂停以避免返送" : call.Flag("system_audio_active") ? "已加入" : "未加入")}";
        }
        else if (stream.TryGetProperty("media", out var media) && media.Text("codec").Length > 0)
        {
            if (codecWindows.TryGetValue(card.Key, out var codecWindow)) codecWindow.UpdateTelemetry(media);
            card.Media = $"{media.Text("protocol", "A2DP")} / {CodecName(media.Text("codec"))} · {media.Number("rate") / 1000:0.###} kHz · {Bitrate(media.Number("bitrate"))}";
            if (media.Flag("protected")) card.PlaybackNotice = "链路不稳 · 已暂时隔离碎音，建议降低码率";
            if (media.Text("protocol") == "LE Audio")
                card.LiveDetails = $"{(media.Flag("playing") ? "媒体播放中" : "媒体未播放")} · LC3 同步音频" + DetailMetric(media, "packets_per_second", "", " 包/s") + DetailMetric(media, "buffer_ms", "实际缓冲 ", " ms") + DetailMetric(media, "concealed_blocks", "补偿 ", " 帧", "0") + DetailMetric(media, "presentation_ms", "协商播放时延 ", " ms") + "\n" + VoiceCodecStatus;
            else card.LiveDetails = $"{(card.CanAnswer ? card.CallDescription : media.Flag("playing") ? "媒体播放中" : "媒体未播放")}" + DetailMetric(media, "packets_per_second", "", " 包/s") + DetailMetric(media, "buffer_ms", "实际缓冲 ", " ms") + DetailMetric(media, "recoveries", "已重同步 ", " 次", "0") + DetailMetric(media, "lost_packets", "RTP 丢包 ", "", "0") + "\n" + VoiceCodecStatus;
        }
        else if (!card.CanAnswer && !card.CanHangup) { card.Media = "媒体编码尚未协商"; card.LiveDetails = "已连接 · 媒体未播放\n" + VoiceCodecStatus; }
    }
    private void SetRunning(bool value)
    {
        StatusDot.Fill = new System.Windows.Media.SolidColorBrush(value ? System.Windows.Media.Color.FromRgb(49, 139, 107) : System.Windows.Media.Color.FromRgb(152, 161, 175));
        running = value;
        if (!value && !driverBusy && pendingSetupMode is { } desired) { pendingSetupMode = null; _ = ChooseMode(desired, false); }
        StartButton.Content = value ? "停止接收" : "开始接收"; StartButton.IsEnabled = !driverBusy;
        foreach (Control control in new Control[] { NameEntry, CallsToggle, ModePicker, AdapterPicker, MediaBufferPicker, CallBufferPicker, MaximumPicker, LdacToggle, Lhdc5Toggle, LeAudioToggle, ConnectionPolicyPicker }) control.IsEnabled = !value && !driverBusy;
        if (!value) { pending.Clear(); StatusTitle.Text = "接收已停止"; StatusDetail.Text = "点击开始接收，即可再次连接移动设备。"; }
        SettingsHint.Text = value ? "音量与声音设备可实时调整；其它连接设置先停止接收再修改，修改自动保存。" : "修改自动保存。";
        SetPickerStates(); UpdateSetupState(); UpdateProtocolAvailability();
        DriverStopButton.IsEnabled = !stopping;
        if (stopping || starting) StartButton.IsEnabled = false;
    }
    private void SetPickerStates()
    {
        if (OutputPicker == null) return;
        OutputPicker.IsEnabled = settings.mode != "native" && !pending.ContainsKey("output") && !driverBusy && !refreshingAudio;
        InputPicker.IsEnabled = settings.mode != "native" && (!running || !sessionMediaOnly) && !pending.ContainsKey("input") && !driverBusy && !refreshingAudio;
        SystemPicker.IsEnabled = settings.mode != "native" && (!running || !sessionMediaOnly) && !pending.ContainsKey("system") && !driverBusy && !refreshingAudio;
        MuteToggle.IsEnabled = settings.mode != "native" && (!running || !sessionMediaOnly) && !driverBusy;
        PlaybackGain.IsEnabled = MicrophoneGain.IsEnabled = settings.mode != "native" && !driverBusy;
        MicrophoneGain.IsEnabled &= !running || !sessionMediaOnly;
        SystemGain.IsEnabled = settings.mode != "native" && !driverBusy;
        SystemGain.IsEnabled &= !running || !sessionMediaOnly;
        SystemGain.CanAdjust = settings.system_audio_enabled;
        SystemAudioToggle.IsEnabled = settings.mode != "native" && (!running || !sessionMediaOnly) && settings.calls && !driverBusy && systemAudioRequest == null;
        foreach (Control control in new Control[] { NameEntry, CallsToggle, MediaBufferPicker, CallBufferPicker, MaximumPicker, LdacToggle, Lhdc5Toggle, LeAudioToggle, ConnectionPolicyPicker })
            control.IsEnabled = !running && !driverBusy && settings.mode != "native";
        VoiceCodecHint.Text = settings.mode == "native" ? VoiceCodecStatus : VoiceCodecStatus + "；用于通话及支持蓝牙麦克风的录音、录像应用";
        if (!running) SettingsHint.Text = settings.mode == "native" ? "共用蓝牙模式：设备名称、媒体编码、播放设备和音量由 Windows 管理，不提供蓝牙语音与麦克风。" : "";
        AdapterPicker.IsEnabled = settings.mode != "native" && !running && !driverBusy;
        UpdateDriverActions(); UpdateMixState(); UpdateMediaControls(); UpdateSetupState(); UpdateProtocolAvailability();
    }
    private bool VoiceBusy => voiceInUse || State.Devices.Any(card => card.CanAnswer || card.CanHangup);
    private void UpdateMediaControls()
    {
        if (CodecPicker == null || PlaybackModePicker == null) return;
        var dedicated = settings.mode != "native";
        CodecPicker.IsEnabled = CodecSettingsButton.IsEnabled = dedicated && !driverBusy;
        PlaybackModePicker.IsEnabled = dedicated && !driverBusy && !mediaModePending;
        foreach (var card in State.Devices) card.CanSelectMedia = dedicated && settings.media_mode == "single" && State.Devices.Count >= 2;
        MediaModeHint.Text = !dedicated ? "此模式的媒体连接与编码由 Windows 管理。" : settings.media_mode == "mix"
            ? "一起听：各设备声音同时播放，可分别调节音量。" : "当前设备停止传音约 0.8 秒后自动接替等待播放的设备；点击设备名旁的标记可立即切换。语音使用中不自动切换媒体。";
        if (dedicated && State.Devices.Count >= 2) MediaModeHint.Text = $"已连接 {State.Devices.Count} 台 · " + MediaModeHint.Text;
        var desired = settings.codec;
        CodecHint.Text = !dedicated ? "此模式的媒体编码和码率由 Windows 管理，不提供蓝牙语音与麦克风。" : (desired switch
        {
            "auto" => "优先协商可用高清媒体编码；多设备断续时可切换 AAC。",
            "aac" => "AAC 最高 320 kb/s，兼顾音质与带宽。实际码率由移动设备协商。",
            "aac_192" => "AAC 最高 192 kb/s，减少无线带宽占用，适合多设备或信号不稳时使用。",
            "sbc" => "SBC 兼容性较好，实际码率由移动设备协商；不一定比 AAC 更低。",
            "ldac" => "LDAC 高清解码；并发更占带宽，建议仅一个设备出声。",
            "lhdc5" => "LHDC 5.0 高清媒体解码；需要发送设备支持。采样率与实际码率显示在设备卡片上。",
            _ => "高清媒体编码更占无线带宽，多设备同时播放可能丢包；建议只听一台。"
        }) + " 首选与允许列表分开；修改自动保存，用于下次设备连接。当前设备可从“…” →“编码与音质”单独调整。语音编码自动协商。";
        var high = settings.codec is "aptx" or "aptx_hd" or "ldac" or "lhdc5" || settings.codec == "auto" && highMediaActive;
        MultipointNotice.Visibility = dedicated && State.Devices.Count >= 2 && settings.media_mode == "mix" && mediaProtected ? Visibility.Visible : Visibility.Collapsed;
        MultipointText.Text = settings.media_mode == "single" ? $"已连接 {State.Devices.Count} 台 · 当前只听一个设备。需要一起听时，在上方“多设备播放”选择“同时播放”。"
            : mediaProtected ? "检测到持续丢包，已隔离不稳定一路的碎音。可从设备的“…” →“编码与音质”改用 AAC，或选择单台自动接替。"
            : high ? "已连接多台 · 高清并发可能断续。可从各设备的“…” →“编码与音质”分别改用 AAC。"
            : $"已连接 {State.Devices.Count} 台 · 当前同时出声。若出现断续，可进一步降低 AAC 码率。";
        LowerBitrateButton.Visibility = settings.media_mode == "mix" && settings.codec != "aac_192" ? Visibility.Visible : Visibility.Collapsed;
        LowerBitrateButton.Content = "逐台调整编码…";
        LowerBitrateButton.IsEnabled = !VoiceBusy && !driverBusy;
    }
    private void PlaybackSettingsClick(object sender, RoutedEventArgs e) => ShowAdvancedAudio();
    private void CodecChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || CodecPicker?.SelectedItem is not ComboBoxItem selected) return;
        settings.codec = selected.Tag?.ToString() ?? "auto";
        QueueSettings(); UpdateMediaControls();
    }
    private async void PlaybackModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || PlaybackModePicker?.SelectedItem is not ComboBoxItem selected) return;
        var mode = selected.Tag?.ToString() ?? "mix";
        if (mode == settings.media_mode) return;
        mediaModePending = true; UpdateMediaControls();
        if (!await Send(new { command = "media_mode", mode })) { mediaModePending = false; ApplySettings(); }
    }
    private async void SelectMediaClick(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: DeviceCard card }) await Send(new { command = "device_action", action = "select_media", key = card.Key });
    }
    private void LowerBitrateClick(object sender, RoutedEventArgs e)
    {
        if ((State.Devices.FirstOrDefault(card => card.IsMediaSelected) ?? State.Devices.FirstOrDefault()) is { } card) OpenDeviceCodec(card);
    }
    internal async Task ApplyCodecProfile(string? force = null)
    {
        if (driverBusy || settings.mode == "native") return;
        if (force != null) { settings.codec = force; var previous = applying; applying = true; SelectTag(CodecPicker, force); applying = previous; }
        QueueSettings(); await FlushSettings();
    }
    private void UpdateAudioFooterLayout()
    {
        if (DedicatedAudioFooter == null) return;
        bool wrap = SystemAudioToggle.IsChecked == true && DedicatedAudioFooter.ActualWidth < 720;
        Grid.SetColumnSpan(MasterControls, wrap ? 2 : 1);
        Grid.SetRow(SystemAudioOptionPanel, wrap ? 1 : 0); Grid.SetColumn(SystemAudioOptionPanel, wrap ? 1 : 2);
        Grid.SetColumnSpan(SystemAudioOptionPanel, wrap ? 2 : 1);
        SystemAudioOptionPanel.HorizontalAlignment = wrap ? HorizontalAlignment.Right : HorizontalAlignment.Stretch;
        SystemAudioOptionPanel.Margin = wrap ? new Thickness(0, 7, 0, 0) : new Thickness(10, 0, 0, 0);
    }
    private void UpdateMixState()
    {
        UpdateAudioFooterLayout();
        PlaybackGain.Secondary = (OutputPicker.SelectedItem as AudioChoice)?.Name ?? "默认播放设备（自动跟随）";
        MicrophoneGain.Secondary = settings.microphone_muted ? "发送到设备：已静音" : (InputPicker.SelectedItem as AudioChoice)?.Name ?? "默认麦克风";
        MutedIndicator.Visibility = settings.mode != "native" && settings.microphone_muted ? Visibility.Visible : Visibility.Collapsed;
        if (settings.mode == "native")
        { TransmitHint.Text = "共用蓝牙模式：媒体播放、编码和码率由 Windows 管理"; SystemAudioStatus.Text = "此模式不提供蓝牙麦克风"; return; }
        if (running && sessionMediaOnly)
        { TransmitHint.Text = "此适配器当前仅接收媒体，电脑麦克风不可用"; SystemAudioStatus.Text = "此适配器当前不提供语音传输"; return; }
        TransmitHint.Text = settings.microphone_muted ? "发送已暂停：麦克风与电脑声音均不发送" : "开启后停止发送麦克风和电脑声音，仍可听到移动设备。";
        SystemAudioStatus.Text = settings.system_audio_enabled ? systemFeedbackBlocked ? "电脑声音已暂停：采集来源与 BlueHead 播放设备相同。请在上方选择“全部应用声音（排除 BlueHead）”，或使用另一个播放设备。麦克风继续工作。" : systemAudioActive ? "电脑声音：正在发送到设备" : "电脑声音：使用蓝牙麦克风时加入" : "电脑声音：未加入";
        SystemGain.Secondary = (SystemPicker.SelectedItem as AudioChoice)?.Name ?? "全部应用声音（排除 BlueHead）";
    }
    private async void StartClick(object sender, RoutedEventArgs e)
        => await StartReception(automatic: false);
    private async Task StartReception(bool automatic)
    {
        if (stopping || starting || closing || (automatic && !receptionRequested)) return;
        if (running)
        {
            if (automatic) return;
            receptionRequested = false; BeginStopping(); await Send(new { command = "stop" }); return;
        }
        if (driverChecking || driverBusy || closing) return;
        if (!automatic) receptionRequested = true;
        if (!NeedsBluetoothSetup) await RefreshDrivers();
        if (NeedsBluetoothSetup) { if (!automatic) SetupBluetoothClick(StartButton, new RoutedEventArgs()); else ShowPage("adapters"); return; }
        if (!await FlushSettings()) return;
        var selected = ReadSettings();
        starting = true; SetRunning(true); StatusTitle.Text = "正在准备接收";
        try
        {
            await EnsureBackend();
            settings = selected; ApplySettings();
            if (!await Send(new { command = "start", settings = selected })) SetRunning(false);
        }
        catch (Exception error) { ApplyEngineFailure(engine.Generation, error.Message); }
        finally
        {
            starting = false; if (!stopping) StartButton.IsEnabled = !driverBusy && !closing;
        }
    }
    private async Task SelectAudio(string direction, AudioChoice? choice)
    {
        if (applying || choice is null) return;
        if (direction == "system" ? choice.EndpointId == settings.system_endpoint_id : choice.Index == (direction == "output" ? settings.output_device : settings.input_device)) return;
        var id = ++requestId; pending[direction] = id; SetPickerStates();
        if (!await Send(new { command = "audio_device", direction, device = choice.Index, endpoint_id = choice.EndpointId, request_id = id }))
        { pending.Remove(direction); ApplySettings(); }
    }
    private async void OutputChanged(object sender, SelectionChangedEventArgs e) => await SelectAudio("output", OutputPicker.SelectedItem as AudioChoice);
    private async void InputChanged(object sender, SelectionChangedEventArgs e) => await SelectAudio("input", InputPicker.SelectedItem as AudioChoice);
    private async void SystemSourceChanged(object sender, SelectionChangedEventArgs e) => await SelectAudio("system", SystemPicker.SelectedItem as AudioChoice);
    private async void MuteChanged(object sender, RoutedEventArgs e)
    { if (!applying) { settings.microphone_muted = MuteToggle.IsChecked == true; UpdateMixState(); await Send(new { command = "mute", muted = settings.microphone_muted }); } }
    private void AdvancedMixClick(object sender, RoutedEventArgs e) => ShowAdvancedAudio();
    internal void ShowAdvancedAudio()
    {
        ShowPage("settings"); AdvancedAudio.IsExpanded = true; UpdateLayout();
        SettingsPage.ScrollToVerticalOffset(SettingsPage.VerticalOffset + AdvancedAudio.TranslatePoint(new Point(0, 0), SettingsPage).Y - 12);
    }
    private async void SystemAudioChanged(object sender, RoutedEventArgs e)
    {
        UpdateAudioFooterLayout();
        if (applying) return;
        var id = ++requestId;
        systemAudioRequest = id;
        SystemAudioToggle.IsEnabled = false;
        if (!await Send(new { command = "system_audio", enabled = SystemAudioToggle.IsChecked == true, request_id = id }))
        { systemAudioRequest = null; ApplySettings(); }
    }
    private async Task<bool> SaveSettings()
    {
        if (running) { ShowNotice("请先停止接收，再保存连接设置。"); return false; }
        if (!await Send(new { command = "configure", settings = ReadSettings() })) return false;
        settingsDirty = false; settingsTimer.Stop(); SaveDesktopPreferences();
        return true;
    }
    private async void ModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || ModePicker?.SelectedItem is not ComboBoxItem item) return;
        await ChooseMode(item.Tag?.ToString() ?? settings.mode, false);
    }
    private void AdapterChoiceChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || running || driverBusy) return;
        if (AdapterPicker.SelectedItem is AdapterChoice adapter) settings.transport = adapter.Transport;
        UpdateSetupState();
    }
    private async void RefreshClick(object sender, RoutedEventArgs e) { await RefreshDrivers(); await Send(new { command = "inventory" }); }
    private static DeviceCard? Card(object sender) => (sender as FrameworkElement)?.DataContext as DeviceCard;
    private void QueueGain(string key, object message)
    { queuedGains[key] = message; if (!gainTimer.IsEnabled) gainTimer.Start(); }
    private async Task FlushGains()
    {
        gainTimer.Stop();
        var commands = queuedGains.Values.ToArray(); queuedGains.Clear();
        foreach (var message in commands) await Send(message);
    }
    private void DeviceGain(object sender, double value, string direction)
    {
        if (applying || Card(sender) is not { } card) return;
        var gain = FrameworkCompat.Clamp(value / 100, 0, 2);
        if (!settings.device_volumes.TryGetValue(card.Key, out var volumes)) settings.device_volumes[card.Key] = volumes = new();
        if (direction == "down") volumes.down = gain; else volumes.up = gain;
        QueueGain(card.Key + direction, new { command = "gain", key = card.Key, direction, gain });
    }
    private void GainChanged(object sender, RoutedPropertyChangedEventArgs<double> e) => DeviceGain(sender, e.NewValue, "down");
    private void UplinkGainChanged(object sender, RoutedPropertyChangedEventArgs<double> e) => DeviceGain(sender, e.NewValue, "up");
    private void MasterGainChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (applying || sender is not AudioLevel level) return;
        var bus = level.Tag?.ToString(); var gain = FrameworkCompat.Clamp(e.NewValue / 100, 0, 2);
        if (bus == "playback") settings.playback_gain = gain;
        else if (bus == "microphone") settings.microphone_gain = gain;
        else if (bus == "system") settings.system_gain = gain;
        else return;
        QueueGain("master:" + bus, new { command = "master_gain", bus, gain });
    }
    private void DeviceMenuClick(object sender, RoutedEventArgs e)
    { if (sender is Button { ContextMenu: { } menu } button) { menu.PlacementTarget = button; menu.IsOpen = true; } }
    private async Task Act(object sender, string action) { if (Card(sender) is { } card) await Send(new { command = "device_action", action, key = card.Key }); }
    private async void AnswerClick(object sender, RoutedEventArgs e) => await Act(sender, "answer");
    private async void DisconnectClick(object sender, RoutedEventArgs e) => await Act(sender, "disconnect");
    private async void RemoteMediaClick(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { Tag: string action } element || Card(element) is not { } card) return;
        if (!devices.TryGetValue(card.Key, out var current) || !ReferenceEquals(card, current)) return;
        var allowed = action switch { "previous" => card.CanPrevious, "next" => card.CanNext, "play" or "pause" => card.CanPlayPause && action == card.PlaybackAction, _ => false };
        if (allowed) await Act(sender, action);
    }
    private Task RefreshDrivers()
    {
        if (offline) return Task.CompletedTask;
        if (driverRefreshTask == null || driverRefreshTask.IsCompleted) driverRefreshTask = ReadDriverInventory();
        return driverRefreshTask;
    }
    private async Task ReadDriverInventory()
    {
        driverChecking = true; UpdateDriverActions(); UpdateSetupState();
        try
        {
            var snapshot = await Task.Run(() => (Devices: Drivers.Enumerate(), SystemReady: Drivers.SystemRadioAvailable()));
            ApplyDriverInventory(snapshot.Devices, snapshot.SystemReady);
        }
        catch (Exception error) { driverInventoryError = "蓝牙状态检查失败：" + error.Message; DriverStatus.Text = driverInventoryError; }
        finally { driverChecking = false; UpdateDriverActions(); UpdateSetupState(); }
    }
    internal void ApplyDriverInventory(List<DriverDevice> inventory, bool? windowsRadioReady = null)
    {
        var selected = preparedDriverInstance ?? (DriverPicker.SelectedItem as DriverDevice)?.InstanceId;
        driverInventory = inventory; driversInspected = true; driverInventoryError = null;
        systemRadioReady = windowsRadioReady ?? inventory.Any(device => !BluetoothSetup.Dedicated(device) && !device.RebootRequired && (device.SystemRadioReady ?? device.Healthy));
        var previous = applying; applying = true;
        DriverPicker.ItemsSource = inventory;
        DriverPicker.SelectedItem = inventory.FirstOrDefault(item => item.InstanceId == selected) ?? inventory.FirstOrDefault();
        string? transport = (AdapterPicker.SelectedItem as AdapterChoice)?.Transport;
        var adapters = AdapterPicker.Items.OfType<AdapterChoice>().Select(LabelAdapter).ToArray();
        AdapterPicker.ItemsSource = adapters;
        AdapterPicker.SelectedItem = adapters.FirstOrDefault(item => item.Transport == transport);
        applying = previous;
        UpdateDriverActions(); UpdateSetupState();
    }
    private void UpdateDriverActions()
    {
        var device = DriverPicker.SelectedItem as DriverDevice;
        var guide = BluetoothSetup.Adapter(device);
        bool idle = !running && !stopping && !starting && !driverBusy && !driverChecking && !closing;
        bool dedicated = BluetoothSetup.Dedicated(device);
        bool known = device != null;
        ModePicker.IsEnabled = HomeModePicker.IsEnabled = idle;
        HomeModePicker.ToolTip = running ? "先点击右上角“停止接收”，再选择模式。" : "Windows 共用：仅收音乐／视频，保留系统蓝牙连接；BlueHead 专用：电脑麦克风、媒体编码选择与多设备同时连接。";
        DriverPicker.IsEnabled = idle && driverInventory.Count > 0;
        DriverPicker.Visibility = driverInventory.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        AdapterSelectionHelp.Text = driverInventory.Count == 0 ? systemRadioReady ? "Windows 蓝牙可用，但没有可切换驱动的 USB 蓝牙设备。可以使用基础模式；进阶模式需插入支持的 USB 蓝牙适配器。" : "没有检测到电脑蓝牙。请插入 USB 蓝牙适配器，再点右上角“刷新状态”；若电脑有内置蓝牙，请确认已在 Windows 中打开。" : "这里选电脑内置或 USB 蓝牙，不是要连接的手机／平板。多个蓝牙设备时，只修改你选的这一个。";
        DriverStateTitle.Text = "3  " + guide.State;
        DriverStatus.Text = driverInventoryError ?? guide.Detail;
        DriverInstallButton.Visibility = known && !dedicated && !device!.RebootRequired && RequestedMode != "native" ? Visibility.Visible : Visibility.Collapsed;
        DriverRestoreButton.Visibility = known && (dedicated || guide.CanRestore) ? Visibility.Visible : Visibility.Collapsed;
        DriverRestoreButton.Content = "恢复 Windows 蓝牙";
        DriverRestoreButton.IsEnabled = idle && guide.CanRestore;
        DriverInstallButton.IsEnabled = idle && guide.CanInstall;
        OriginalBootstrapButton.Visibility = dedicated && device?.RebootRequired == false && !HasPendingMode ? Visibility.Visible : Visibility.Collapsed;
        OriginalBootstrapButton.IsEnabled = idle && dedicated && device?.HasBackup == true && (SelectedControllerGuide?.CanOriginal ?? true);
        WindowsPairButton.Visibility = RequestedMode == "native" && !dedicated && systemRadioReady ? Visibility.Visible : Visibility.Collapsed;
        WindowsPairButton.IsEnabled = !driverBusy;
        ApplyModeButton.Visibility = HasPendingMode && CanUseMode(RequestedMode) ? Visibility.Visible : Visibility.Collapsed;
        ApplyModeButton.IsEnabled = idle;
        OriginalBootstrapButton.Appearance = SelectedControllerGuide != null && RequestedMode != "native" ? Wpf.Ui.Controls.ControlAppearance.Primary : Wpf.Ui.Controls.ControlAppearance.Secondary;
        DriverRestoreButton.Appearance = RequestedMode == "native" && dedicated ? Wpf.Ui.Controls.ControlAppearance.Primary : Wpf.Ui.Controls.ControlAppearance.Secondary;
        DriverStopButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        DriverActionHint.Text = driverBusy ? "正在处理，请等待。不要拔出蓝牙设备或关闭电脑。" : running || stopping || starting ? "请先停止接收，再修改模式或驱动。停止不会取消配对。" : device?.RebootRequired == true ? "驱动操作已完成，请在 Windows 开始菜单选择“重启”。重启前无需重复切换。" : dedicated ? device?.HasBackup == true ? "“修复专用蓝牙”用于蓝牙无法启动；“恢复 Windows 蓝牙”用于重新使用系统蓝牙。点击后才会请求管理员授权。" : "没有可用原厂驱动备份。需要恢复时，请安装电脑或蓝牙设备厂商提供的驱动；可使用下方独立恢复工具。" : RequestedMode != "native" && known ? "点击“备份并启用专用蓝牙”才会更换驱动，并请求管理员授权。只改上方选项不会切换。" : "基础模式无需修改驱动。在本页底部右侧点击“打开 Windows 蓝牙”配对设备，然后回音频中心点击右上角“开始接收”。";
        UpdateControllerActions(); UpdateModeChoice();
    }
    private void DriverSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!applying && !running && DriverPicker.SelectedItem is DriverDevice device)
        {
            var choices = AdapterPicker.Items.OfType<AdapterChoice>().Where(choice => Drivers.MatchTransport(choice.Transport, new[] { device }).Count == 1).ToArray();
            if (BluetoothSetup.Dedicated(device) && choices.Length == 1)
            {
                applying = true; AdapterPicker.SelectedItem = choices[0]; applying = false;
                settings.transport = choices[0].Transport;
            }
            if (driverResultInstance != null && driverResultInstance != device.InstanceId && !driverBusy) DriverResult.Text = "";
        }
        UpdateDriverActions(); UpdateSetupState();
    }
    private async Task DriverOperation(string operation)
    {
        if (running || stopping || starting || driverBusy || closing) { ShowNotice("请先在音频中心点击右上角“停止接收”，再切换模式或驱动。"); return; }
        var requested = (DriverPicker.SelectedItem as DriverDevice)?.InstanceId;
        if (requested == null) { ShowNotice("未找到蓝牙适配器，请连接后刷新状态。"); return; }
        await RefreshDrivers();
        if (running || stopping || driverBusy || closing) return;
        if (driverInventoryError != null) { DriverResult.Text = "蓝牙状态读取失败，请刷新后重试。"; return; }
        if (DriverPicker.SelectedItem is not DriverDevice device || !string.Equals(device.InstanceId, requested, StringComparison.OrdinalIgnoreCase))
        { DriverResult.Text = "蓝牙适配器已变化，请重新选择。"; return; }
        var guide = BluetoothSetup.Adapter(device);
        if (operation == "initialize-original" ? !OriginalBootstrapButton.IsEnabled : operation == "install" ? RequestedMode == "native" || !guide.CanInstall : !guide.CanRestore) return;
        if (offline) { SelfTestDriverOperations.Add(operation); if (SelfTestDriverRunner == null) return; }
        driverResultInstance = device.InstanceId; driverDiagnostic = null; Notice.Visibility = Visibility.Collapsed;
        driverBusy = true; SetDriverActivity(operation, true); SetRunning(false); DriverProgress.Visibility = Visibility.Visible;
        DriverResult.Text = "";
        try
        {
            int exitCode; DriverRecord? record;
            // Paint the permission phase before Windows opens its UAC prompt.
            await Task.Delay(30);
            if (offline)
            {
                SetDriverActivity(operation);
                (exitCode, record) = await SelfTestDriverRunner!(operation, device);
            }
            else
            {
                var path = Path.Combine(AppContext.BaseDirectory, "BlueHead.DriverHelper.exe");
                var start = new ProcessStartInfo(path) { UseShellExecute = true, Verb = "runas", WorkingDirectory = Path.GetDirectoryName(path), WindowStyle = ProcessWindowStyle.Hidden };
                start.AddArgument("--" + operation); start.AddArgument(device.InstanceId);
                using var helper = Process.Start(start) ?? throw new IOException("恢复工具未能启动。");
                SetDriverActivity(operation);
                await helper.WaitForExitAsync(); exitCode = helper.ExitCode;
                record = Drivers.ReadRecord(device.InstanceId);
            }
            var feedback = BluetoothSetup.DriverCompletion(operation, exitCode, record);
            driverDiagnostic = record?.Error;
            if (feedback.Completed)
            {
                ClearControllerFailure(); ApplySuccessfulDriverOperation(feedback.EffectiveOperation, device);
                await Send(new { command = "configure", settings });
            }
            DriverResult.Text = feedback.Message;
            SetUsageOutcome(feedback.Completed ? feedback.Ready ? "已改用" + BluetoothSetup.UsageName(settings.mode) : "驱动操作完成，还需处理" : "驱动操作未完成", feedback.Message, !feedback.Ready);
            DriverResult.Foreground = new System.Windows.Media.SolidColorBrush(feedback.Ready ? System.Windows.Media.Color.FromRgb(7, 139, 78) : System.Windows.Media.Color.FromRgb(167, 53, 69));
            receptionRequested = false;
            if (!feedback.Ready) ShowNotice(feedback.Message);
        }
        catch (Win32Exception error) when (error.NativeErrorCode == 1223)
        {
            receptionRequested = false;
            if (operation != "initialize-original") CancelModeChoice();
            DriverResult.Text = operation == "initialize-original" ? "已取消恢复。需要时可点击“修复专用蓝牙”重试。" : "已取消切换，当前模式未改变。";
            SetUsageOutcome("已取消驱动操作", DriverResult.Text);
            ShowNotice(DriverResult.Text);
        }
        catch (Exception error)
        {
            driverDiagnostic = error.Message;
            DriverResult.Foreground = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(167, 53, 69));
            DriverResult.Text = BluetoothSetup.DriverFailure(operation, error.Message);
            SetUsageOutcome("驱动操作未完成", DriverResult.Text, true);
            ShowNotice(DriverResult.Text);
        }
        finally
        {
            SetDriverActivity(null); driverBusy = false; DriverProgress.Visibility = Visibility.Collapsed; SetRunning(false);
            await RefreshDrivers();
            await Send(new { command = "inventory" });
        }
        // Configuration and recovery never start reception. Keep the user in
        // this page with a clear result, then let Home own the Start action.
        UpdateDriverActions(); UpdateSetupState();
    }
    internal Func<string, DriverDevice, Task<(int ExitCode, DriverRecord? Record)>>? SelfTestDriverRunner { get; set; }
    internal List<string> SelfTestDriverOperations { get; } = [];
    internal void ApplySuccessfulDriverOperation(string operation, DriverDevice device)
    {
        preparedDriverInstance = operation == "install" ? device.InstanceId : null;
        if (operation == "install") SetIntendedMode("advanced");
        else
        {
            var transport = (AdapterPicker.SelectedItem as AdapterChoice)?.Transport ?? settings.transport;
            var matches = Drivers.MatchTransport(transport, driverInventory);
            bool selected = matches.Count == 1 && matches[0].InstanceId == device.InstanceId;
            bool another = driverInventory.Any(other => other.InstanceId != device.InstanceId && BluetoothSetup.Dedicated(other) && other.Healthy && !other.RebootRequired);
            if (settings.mode == "native" || selected || !another) SetIntendedMode("native");
        }
    }
    private void RecoveryClick(object sender, RoutedEventArgs e)
    {
        try { var start = new ProcessStartInfo(FrameworkCompat.ProcessPath) { UseShellExecute = true }; start.AddArgument("--recovery"); Process.Start(start); }
        catch (Exception error) { ShowNotice(error.Message); }
    }
    private async void TelemetryClick(object sender, RoutedEventArgs e) => await Send(new { command = "telemetry" });
    private void ExportDiagnosticsClick(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.SaveFileDialog { FileName = "BlueHead-diagnostics.json", Filter = "诊断报告|*.json" };
        if (dialog.ShowDialog(this) != true) return;
        var report = CreateDiagnosticsReport();
        File.WriteAllText(dialog.FileName, JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
        FooterStatus.Text = "诊断报告已导出";
    }
    internal object CreateDiagnosticsReport() =>
        new { version = AppVersion.Number, time_utc = DateTime.UtcNow, os = Environment.OSVersion.VersionString, engine_ready = engine.Ready,
            connected_devices = State.Devices.Count, telemetry = telemetry?.GetProperty("audio"), scope = "Anonymous counters only; no audio, keys, raw logs, device names or addresses" };
    private void ProjectNavigate(object sender, System.Windows.Navigation.RequestNavigateEventArgs e)
    {
        try { Process.Start(new ProcessStartInfo(e.Uri.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception error) { ShowNotice("无法打开项目主页：" + error.Message); }
        e.Handled = true;
    }
    private void OpenDataClick(object sender, RoutedEventArgs e)
    { try { Directory.CreateDirectory(dataDirectory); Process.Start(new ProcessStartInfo(dataDirectory) { UseShellExecute = true }); } catch (Exception error) { ShowNotice(error.Message); } }
    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (offline && !SelfTestExitLifecycle) return;
        if (driverBusy) { e.Cancel = true; ShowNotice("正在处理驱动，请等待操作完成。"); return; }
        if (!offline && preferences.Tray && !allowClose && !closing) { e.Cancel = true; Hide(); return; }
        if (closing) { e.Cancel = true; return; }
        e.Cancel = true; closing = true; timer.Stop(); StartButton.IsEnabled = false; FooterStatus.Text = "正在关闭蓝牙与麦克风…";
        receptionRequested = false;
        IsEnabled = false;
        // An already-dead backend makes every await complete synchronously.
        // Always leave WPF's Closing stack before the final Close; otherwise
        // VerifyNotClosing throws and leaves an invisible mutex-owning window.
        ExitTask = Dispatcher.InvokeAsync(new Func<Task>(CompleteExitAsync), DispatcherPriority.Background).Task.Unwrap();
    }
    private void RecordExitError(Exception error)
    {
        if (offline) { SelfTestExitErrors.Add(error.GetType().Name); return; }
        try
        {
            Directory.CreateDirectory(dataDirectory);
            var path = Path.Combine(dataDirectory, "desktop-errors.log");
            if (File.Exists(path) && new FileInfo(path).Length > 131072)
            { var previous = path + ".1"; if (File.Exists(previous)) File.Delete(previous); File.Move(path, previous); }
            File.AppendAllText(path, JsonSerializer.Serialize(new { time_utc = DateTime.UtcNow, operation = "shutdown", error = error.GetType().Name, message = error.Message }) + Environment.NewLine);
        }
        catch (Exception) { } // Diagnostics cannot interrupt application exit.
    }
    private async Task CompleteExitAsync()
    {
        try { if (offline && SelfTestShutdown != null) await SelfTestShutdown(); }
        catch (Exception error) { RecordExitError(error); }
        try { await FlushGains(); }
        catch (Exception error) { RecordExitError(error); }
        try { await FlushSettings(); }
        catch (Exception error) { RecordExitError(error); }
        try { await engine.ShutdownAsync(); }
        catch (Exception error) { RecordExitError(error); }
        try { await Task.Run(engine.Dispose); }
        catch (Exception error) { RecordExitError(error); }
        try { tray?.Dispose(); }
        catch (Exception error) { RecordExitError(error); }
        // Cleanup failures are recorded locally, never left in a modal dialog
        // that can keep a hidden application alive after the user chooses Exit.
        Closing -= OnClosing;
        Close();
    }
}
