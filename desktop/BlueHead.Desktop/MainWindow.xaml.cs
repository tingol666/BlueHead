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
    internal CheckBox MuteToggle => EditorPart<CheckBox>(MicrophoneGain.FooterContent, "MuteToggle");
    internal TextBlock TransmitHint => EditorPart<TextBlock>(MicrophoneGain.FooterContent, "TransmitHint");
    internal TextBlock SystemAudioStatus => EditorPart<TextBlock>(SystemGain.FooterContent, "SystemAudioStatus");
    internal readonly DesktopState State = new();
    private readonly EngineClient engine = new();
    private EngineSettings settings = new();
    private readonly Dictionary<string, string> audioHealthNotices = new();
    private UiSettings preferences = new();
    private bool applying = true, running, closing, allowClose, driverBusy, refreshingAudio;
    private readonly bool offline;
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
    private bool inventoryReceived;
    private List<DriverDevice> driverInventory = [];
    private bool driversInspected, systemRadioReady, driverChecking;
    private Task? driverRefreshTask;
    private string? driverInventoryError, preparedDriverInstance, pendingSetupMode, driverResultInstance;
    internal List<string> SelfTestExternalLinks { get; } = [];
    private string? codecDraft;
    private bool mediaModePending, voiceInUse, mediaProtected, highMediaActive;
    private EngineSettings? restartWithCodec;
    private System.Windows.Forms.NotifyIcon? tray;
    internal MainWindow(bool offline = false)
    {
        this.offline = offline;
        var portableRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
        dataDirectory = Environment.GetEnvironmentVariable("BLUEHEAD_DATA_DIR") ??
            (File.Exists(Path.Combine(portableRoot, "portable.flag")) ? Path.Combine(portableRoot, "data") :
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "BlueHead", "data"));
        InitializeComponent(); DataContext = State;
        if (!offline) WindowSizing.Track(this);
        LocationChanged += (_, _) => CloseAudioEditors();
        SizeChanged += (_, _) => CloseAudioEditors();
        Deactivated += (_, _) => CloseAudioEditors();
        ShowPage("home");
        timer = new() { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += async (_, _) => { if (engine.Ready && running) await Send(new { command = "telemetry" }); };
        gainTimer = new() { Interval = TimeSpan.FromMilliseconds(120) };
        gainTimer.Tick += async (_, _) => await FlushGains();
        engine.Message += message => Dispatcher.BeginInvoke(() => ApplyEvent(message));
        engine.Failed += message => Dispatcher.BeginInvoke(() => { SetRunning(false); EngineState.Text = "后台已停止"; ShowNotice(message); });
        Closing += OnClosing;
        Loaded += async (_, _) =>
        {
            if (offline) { applying = false; StartButton.IsEnabled = true; return; }
            LoadPreferences(); SetupTray();
            try { await engine.StartAsync(dataDirectory); timer.Start(); }
            catch (Exception error) { ShowNotice(error.Message); EngineState.Text = "后台不可用"; }
        };
    }
    private void LoadPreferences()
    {
        try { preferences = JsonSerializer.Deserialize<UiSettings>(File.ReadAllText(Path.Combine(dataDirectory, "desktop.json"))) ?? new(); }
        catch (Exception error) when (error is IOException or JsonException) { }
        TrayToggle.IsChecked = preferences.Tray; AutoStartToggle.IsChecked = preferences.AutoStart;
    }
    private void SetupTray()
    {
        var icon = Path.Combine(AppContext.BaseDirectory, "bluehead.ico");
        tray = new() { Text = "BlueHead", Icon = File.Exists(icon) ? new System.Drawing.Icon(icon) : System.Drawing.SystemIcons.Application, Visible = true };
        var menu = new System.Windows.Forms.ContextMenuStrip();
        menu.Items.Add("打开 BlueHead", null, (_, _) => Dispatcher.Invoke(ShowFromTray));
        menu.Items.Add("恢复系统蓝牙…", null, (_, _) => Dispatcher.Invoke(() => { ShowFromTray(); ShowPage("adapters"); }));
        menu.Items.Add("退出", null, (_, _) => Dispatcher.Invoke(() => { allowClose = true; Close(); }));
        tray.ContextMenuStrip = menu; tray.DoubleClick += (_, _) => Dispatcher.Invoke(ShowFromTray);
    }
    private void ShowFromTray() { Show(); WindowState = WindowState.Normal; Activate(); }
    internal void ShowPage(string page)
    {
        CloseAudioEditors();
        foreach (var (button, name) in new[] { (NavHome, "home"), (NavSettings, "settings"), (NavAdapters, "adapters"), (NavHelp, "help") })
        {
            button.Background = new System.Windows.Media.SolidColorBrush(name == page ? System.Windows.Media.Color.FromRgb(225, 246, 236) : System.Windows.Media.Colors.Transparent);
            button.Foreground = new System.Windows.Media.SolidColorBrush(name == page ? System.Windows.Media.Color.FromRgb(7, 139, 78) : System.Windows.Media.Color.FromRgb(100, 119, 134));
        }
        HomePage.Visibility = page == "home" ? Visibility.Visible : Visibility.Collapsed;
        SettingsPage.Visibility = page == "settings" ? Visibility.Visible : Visibility.Collapsed;
        AdaptersPage.Visibility = page == "adapters" ? Visibility.Visible : Visibility.Collapsed;
        HelpPage.Visibility = page == "help" ? Visibility.Visible : Visibility.Collapsed;
    }
    private void HomeClick(object sender, RoutedEventArgs e) => ShowPage("home");
    private void SettingsClick(object sender, RoutedEventArgs e) => ShowPage("settings");
    private async void AdaptersClick(object sender, RoutedEventArgs e) { ShowPage("adapters"); await RefreshDrivers(); }
    private void HelpClick(object sender, RoutedEventArgs e) => ShowPage("help");
    private void PairingClick(object sender, RoutedEventArgs e) => ShowPage("help");
    private void ShowNotice(string text) { NoticeText.Text = text; Notice.Visibility = Visibility.Visible; }
    private async Task<bool> Send(object message)
    {
        if (offline) { SelfTestCommands.Add(JsonSerializer.SerializeToElement(message)); return true; }
        try { await engine.SendAsync(message); return true; }
        catch (Exception error) when (error is IOException or InvalidOperationException or ObjectDisposedException)
        { ShowNotice(error.Message); return false; }
    }
    private EngineSettings ReadSettings()
    {
        settings.name = NameEntry.Text.Trim(); settings.calls = CallsToggle.IsChecked == true;
        settings.codec = (CodecPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto"; codecDraft = null;
        if (settings.codec == "ldac") LdacToggle.IsChecked = true;
        settings.mode = (ModePicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "advanced";
        settings.transport = (AdapterPicker.SelectedItem as AdapterChoice)?.Transport ?? settings.transport;
        settings.buffer_ms = int.Parse((MediaBufferPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "60");
        settings.call_buffer_ms = int.Parse((CallBufferPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "30");
        settings.maximum_devices = int.Parse((MaximumPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "3");
        settings.ldac_enabled = LdacToggle.IsChecked == true;
        return settings;
    }
    private void ApplySettings()
    {
        applying = true;
        NameEntry.Text = settings.name; CallsToggle.IsChecked = settings.calls; LdacToggle.IsChecked = settings.ldac_enabled;
        SelectTag(CodecPicker, codecDraft ?? settings.codec); SelectTag(PlaybackModePicker, settings.media_mode); SelectTag(ModePicker, settings.mode);
        SelectTag(MediaBufferPicker, settings.buffer_ms.ToString()); SelectTag(CallBufferPicker, settings.call_buffer_ms.ToString());
        SelectTag(MaximumPicker, settings.maximum_devices.ToString());
        MuteToggle.IsChecked = settings.microphone_muted;
        PlaybackGain.Value = settings.playback_gain * 100;
        MicrophoneGain.Value = settings.microphone_gain * 100;
        SystemGain.Value = settings.system_gain * 100;
        SystemAudioToggle.IsChecked = settings.system_audio_enabled;
        OutputPicker.SelectedItem = State.Outputs.FirstOrDefault(item => item.Index == settings.output_device) ?? State.Outputs.FirstOrDefault();
        InputPicker.SelectedItem = State.Inputs.FirstOrDefault(item => item.Index == settings.input_device) ?? State.Inputs.FirstOrDefault();
        applying = false;
        SetPickerStates();
        foreach (var card in State.Devices) card.CanMix = settings.mode != "native";
        PairingHint.Text = settings.mode == "native" ? "先在 Windows 设置中配对移动设备，然后点击开始接收，在移动设备上播放声音。" : $"点击“开始接收”，然后在移动设备的蓝牙设置中查找 {settings.name}。";
    }
    private static void SelectTag(ComboBox picker, string tag) => picker.SelectedItem = picker.Items.OfType<ComboBoxItem>().FirstOrDefault(item => item.Tag?.ToString() == tag);
    internal void ApplyEvent(JsonElement message)
    {
        var kind = message.Text("event");
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
            var adapters = message.GetProperty("adapters").EnumerateArray().Select(item => new AdapterChoice(item.Text("transport"), item.Text("name"))).ToList();
            AdapterPicker.ItemsSource = adapters;
            var prepared = driverInventory.FirstOrDefault(device => device.InstanceId == preparedDriverInstance);
            var preparedChoices = prepared == null ? Array.Empty<AdapterChoice>() : adapters.Where(item => Drivers.MatchTransport(item.Transport, new[] { prepared }).Count == 1).ToArray();
            AdapterPicker.SelectedItem = preparedChoices.Length == 1 && settings.mode != "native" ? preparedChoices[0] : adapters.FirstOrDefault(item => item.Transport == settings.transport) ?? adapters.FirstOrDefault();
            if (preparedChoices.Length == 1 && settings.mode != "native") { settings.transport = preparedChoices[0].Transport; preparedDriverInstance = null; }
            State.Outputs.Clear(); State.Inputs.Clear();
            State.Outputs.Add(new(null, "默认播放设备（自动跟随）")); State.Inputs.Add(new(null, "默认麦克风（自动跟随）"));
            foreach (var item in message.GetProperty("audio").EnumerateArray())
            {
                var choice = new AudioChoice(item.GetProperty("index").GetInt32(), item.Text("name"), item.Text("endpoint_id"));
                if (item.GetProperty("max_output_channels").GetInt32() >= 2) State.Outputs.Add(choice);
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
            ApplySettings();
            UpdateSetupState();
        }
        else if (kind == "starting") { audioHealthNotices.Clear(); Notice.Visibility = Visibility.Collapsed; SetRunning(true); StatusTitle.Text = "正在准备接收"; StatusDetail.Text = "正在打开蓝牙与音频设备…"; }
        else if (kind == "stopped")
        {
            voiceInUse = mediaProtected = highMediaActive = false;
            SetRunning(false); State.Devices.Clear(); devices.Clear(); systemAudioActive = false; UpdateDevices(); UpdateMixState();
            if (restartWithCodec is { } next)
            {
                restartWithCodec = null; settings = next; codecDraft = null; ApplySettings();
                _ = Send(new { command = "start", settings });
                FooterStatus.Text = "媒体编码已应用 · 请重新连接移动设备";
            }
        }
        else if (kind is "error" or "warning") ShowNotice(message.Text("message"));
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
                else { settings.input_device = message.Endpoint("device"); settings.input_endpoint_id = message.Text("endpoint_id", "") is { Length: > 0 } i ? i : null; }
                FooterStatus.Text = direction == "output" ? "播放设备已切换 · 蓝牙连接保持" : "麦克风已切换 · 蓝牙连接保持";
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
            telemetry = message.Clone();
            var audio = message.GetProperty("audio");
            DiagnosticsText.Text = audio.TryGetProperty("native_mixer", out var native)
                ? $"音频引擎：{(native.GetBoolean() ? "原生 PCM 混音" : "兼容混音")}\n媒体缓冲：{audio.GetProperty("media_buffer_ms")} ms\n语音缓冲：{audio.GetProperty("call_buffer_ms")} ms\n缓冲不足：{audio.GetProperty("underruns")}\n回调竞争：{audio.GetProperty("callback_contentions")}"
                : "后台已就绪，接收尚未启动。";
            systemAudioActive = voiceInUse = mediaProtected = highMediaActive = false;
            if (message.TryGetProperty("devices", out var streams))
            {
                foreach (var stream in streams.EnumerateArray())
                {
                    if (!devices.TryGetValue(stream.Text("key"), out var card)) continue;
                    ApplyLiveAudio(card, stream);
                    if (stream.TryGetProperty("media", out var media)) { mediaProtected |= media.Flag("protected"); highMediaActive |= media.Text("codec") is "aptx_hd" or "aptx" or "ldac"; }
                    if (stream.TryGetProperty("call", out var voice)) voiceInUse |= voice.Flag("active");
                    if (stream.TryGetProperty("call", out var call) && call.Flag("system_audio_active")) systemAudioActive = true;
                }
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
                case "format": card.Media = $"A2DP / {CodecName(message.Text("codec"))} · {message.Number("rate") / 1000:0.###} kHz · — kb/s"; break;
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
    private void UpdateDevices() { DeviceCount.Text = $"{State.Devices.Count} 台设备"; UpdateSetupState(); UpdateMediaControls(); }
    private DriverDevice? ReceptionAdapter
    {
        get
        {
            var transport = (AdapterPicker.SelectedItem as AdapterChoice)?.Transport ?? settings.transport;
            var matches = Drivers.MatchTransport(transport, driverInventory);
            return matches.Count == 1 ? matches[0] : AdapterPicker.SelectedItem == null ? DriverPicker.SelectedItem as DriverDevice : null;
        }
    }
    internal ReceptionGuide ReceptionGuide => BluetoothSetup.Reception(settings.mode == "native", driversInspected,
        driverInventoryError != null, AdapterPicker.SelectedItem is AdapterChoice, ReceptionAdapter,
        systemRadioReady, driverInventory.Any(device => BluetoothSetup.Dedicated(device) && device.HasBackup));
    internal bool AdapterNeedsRestart => ReceptionGuide.Stage == BluetoothSetupStage.RestartRequired;
    internal bool NeedsBluetoothSetup => !ReceptionGuide.Ready;
    private void UpdateModePresentation(ReceptionGuide guide)
    {
        var shared = settings.mode == "native";
        SharedModeNotice.Visibility = shared && (running || guide.Ready) ? Visibility.Visible : Visibility.Collapsed;
        SharedSettingsNotice.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        DedicatedAudioFooter.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedAudioFooter.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        DedicatedNameFields.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedNameFields.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        AdvancedAudio.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        VoiceSettingsRow.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedCodecValue.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        CodecPicker.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        PlaybackModePicker.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        SharedPlaybackValue.Visibility = shared ? Visibility.Visible : Visibility.Collapsed;
        ModeStatusButton.Content = shared ? guide.Ready || running ? "系统共用 ⌄" : "共用待准备 ⌄" : guide.Ready || running ? "专用接收 ⌄" : "专用待准备 ⌄";
        ModeStatusButton.ToolTip = guide.Detail;
        ModeDescription.Text = shared ? "保留 Windows 的耳机、键鼠等连接，收听设备媒体；播放与编码由系统管理。电脑麦克风与混音需专用接收。" : "让电脑成为蓝牙耳机，支持麦克风、编码选择与多设备混音；需要独占一个内置或外置适配器。";
        PairingHint.Text = shared ? "先在 Windows 中配对设备，再开始接收并播放媒体。" : $"开始接收后，在移动设备蓝牙列表连接 {settings.name}。";
        foreach (var card in State.Devices) card.CanMix = !shared;
        HelpSteps.Text = shared ? "1. 在“蓝牙设置”选择共用 Windows 蓝牙；若适配器仍是专用状态，先恢复系统蓝牙。\n\n2. 打开 Windows 蓝牙设置，配对移动设备。\n\n3. 回到首页开始接收，在移动设备播放媒体。电脑麦克风与混音需改用专用接收。" : "1. 在“蓝牙设置”选择声音和麦克风，启用一个专用适配器。\n\n2. 回到首页开始接收；固件会自动检查和初始化。\n\n3. 在移动设备连接显示的耳机名称，并开启媒体与通话音频。录音、录像应用需支持蓝牙麦克风。";
    }
    private void UpdateSetupState()
    {
        var guide = ReceptionGuide;
        UpdateModePresentation(guide);
        if (!inventoryReceived) return;
        var setup = !running && !guide.Ready && guide.Stage != BluetoothSetupStage.Checking;
        SetupCard.Visibility = setup ? Visibility.Visible : Visibility.Collapsed;
        MediaProfileCard.Visibility = setup ? Visibility.Collapsed : Visibility.Visible;
        EmptyState.Visibility = State.Devices.Count == 0 && !setup && guide.Stage != BluetoothSetupStage.Checking ? Visibility.Visible : Visibility.Collapsed;
        StartButton.Content = running ? "停止接收" : guide.Action;
        StartButton.IsEnabled = !driverBusy && !driverChecking && !closing && (running || !refreshingAudio && guide.Stage != BluetoothSetupStage.Checking);
        SetupTitle.Text = guide.Title;
        SetupDescription.Text = driverInventoryError ?? guide.Detail;
        SetupAction.Content = guide.Action;
        SetupBenefits.Visibility = settings.mode != "native" && guide.Stage is BluetoothSetupStage.SelectAdapter or BluetoothSetupStage.DedicatedRequired ? Visibility.Visible : Visibility.Collapsed;
        SetupCaption.Text = guide.Stage == BluetoothSetupStage.RestartRequired ? "请在 Windows 开始菜单选择“重启”，然后重新打开 BlueHead。" : settings.mode == "native" ? "需要电脑麦克风？改用专用接收；系统共用方式只接收媒体。" : "只需收听媒体时，也可选择共用 Windows 蓝牙。";
        FinishBluetoothButton.IsEnabled = guide.Ready && !running && !driverBusy && !driverChecking;
        FinishBluetoothHint.Text = running ? "先停止接收，再切换方式或准备适配器。" : guide.Ready ? "准备已完成，返回首页即可开始接收。" : guide.Detail;
        DriverStopButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        if (running) return;
        StatusTitle.Text = guide.Title;
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
        foreach (var editor in AudioEditors()) if (editor != except) editor.SetCurrentValue(AudioLevel.IsOpenProperty, false);
        foreach (var card in State.Devices) if (card != detailsExcept) card.DetailsOpen = false;
    }
    private void DeviceDetailsOpened(object? sender, EventArgs e)
    {
        if (sender is System.Windows.Controls.Primitives.Popup { Tag: DeviceCard card }) CloseAudioEditors(detailsExcept: card);
    }
    private async void SetupBluetoothClick(object sender, RoutedEventArgs e)
    {
        if (ReferenceEquals(sender, SetupAction) || ReferenceEquals(sender, StartButton))
        {
            if (ReceptionGuide.Stage == BluetoothSetupStage.SystemBluetoothRequired) { OpenSettings("ms-settings:bluetooth"); return; }
            if (ReceptionGuide.Stage == BluetoothSetupStage.CheckFailed) { await RefreshDrivers(); await Send(new { command = "inventory" }); return; }
        }
        ShowPage("adapters"); await RefreshDrivers();
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
        if (running) { pendingSetupMode = "advanced"; DriverResult.Text = "请先点击“停止接收以切换”，再准备专用接收。"; return; }
        SetIntendedMode("advanced"); await RefreshDrivers();
    }
    private async void StopForSetupClick(object sender, RoutedEventArgs e) { if (running && !driverBusy) await Send(new { command = "stop" }); }
    private void SetIntendedMode(string mode)
    {
        if (settings.mode != mode)
        {
            CloseAudioEditors(); FooterStatus.Text = "本机蓝牙 · 无需同一 Wi-Fi";
            driverResultInstance = null; DriverResult.Text = "";
            Notice.Visibility = Visibility.Collapsed; audioHealthNotices.Clear();
        }
        settings.mode = mode;
        applying = true; SelectTag(ModePicker, mode); applying = false;
        SetPickerStates(); UpdateSetupState();
    }
    private async void FinishBluetoothClick(object sender, RoutedEventArgs e)
    {
        if (NeedsBluetoothSetup) { DriverResult.Text = ReceptionGuide.Detail; return; }
        if (await SaveSettings()) ShowPage("home");
    }
    private static string CodecName(string codec) => codec.ToLowerInvariant() switch
    { "aptx" => "aptX", "aptx_hd" => "aptX HD", "msbc" => "mSBC", "cvsd" => "CVSD", "lc3_swb" or "lc3-swb" => "LC3-SWB", _ => codec.ToUpperInvariant() };
    private static string Bitrate(double bits) => bits >= 1000000 ? $"{bits / 1000000:0.##} Mb/s" : $"{bits / 1000:0.#} kb/s";
    private string VoiceCodecStatus => settings.mode == "native" ? "系统共用不提供蓝牙语音；电脑麦克风需切换专用接收"
        : settings.calls ? "语音编码：自动协商" : "语音编码：自动协商（语音与麦克风已关闭）";
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
            card.Media = $"HFP / {CodecName(call.Text("codec"))} · {call.Number("rate") / 1000:0.###} kHz · ↓ {Bitrate(call.Number("rx_bitrate"))}  ↑ {Bitrate(call.Number("tx_bitrate"))}";
            card.LiveDetails = $"{VoiceCodecStatus} · 当前 {CodecName(call.Text("codec"))} · {call.Number("rate") / 1000:0.###} kHz\n语音通道使用中 · 接收 {call.Number("rx_packets"):0} 包 · 麦克风{(call.Flag("microphone_active") ? "工作中" : "未就绪")} · 电脑声音{(call.Flag("system_audio_active") ? "已加入" : "未加入")}";
        }
        else if (stream.TryGetProperty("media", out var media) && media.Text("codec").Length > 0)
        {
            card.Media = $"A2DP / {CodecName(media.Text("codec"))} · {media.Number("rate") / 1000:0.###} kHz · {Bitrate(media.Number("bitrate"))}";
            if (media.Flag("protected")) card.PlaybackNotice = "链路不稳 · 已暂时隔离碎音，建议降低码率";
            card.LiveDetails = $"{(card.CanAnswer ? card.CallDescription : media.Flag("playing") ? "媒体播放中" : "媒体未播放")} · {media.Number("packets_per_second"):0.#} 包/s · 缓冲 {media.Number("buffer_ms"):0.#} ms · 已重同步 {media.Number("recoveries"):0} 次 · RTP 丢包 {media.Number("lost_packets"):0}\n{VoiceCodecStatus}";
        }
        else if (!card.CanAnswer && !card.CanHangup) { card.Media = "媒体编码尚未协商"; card.LiveDetails = "已连接 · 媒体未播放\n" + VoiceCodecStatus; }
    }
    private void SetRunning(bool value)
    {
        StatusDot.Fill = new System.Windows.Media.SolidColorBrush(value ? System.Windows.Media.Color.FromRgb(49, 139, 107) : System.Windows.Media.Color.FromRgb(152, 161, 175));
        running = value;
        if (!value && !driverBusy && pendingSetupMode is { } desired) { pendingSetupMode = null; SetIntendedMode(desired); }
        StartButton.Content = value ? "停止接收" : "开始接收"; StartButton.IsEnabled = !driverBusy;
        foreach (Control control in new Control[] { NameEntry, CallsToggle, ModePicker, AdapterPicker, MediaBufferPicker, CallBufferPicker, MaximumPicker, LdacToggle, SaveButton }) control.IsEnabled = !value && !driverBusy;
        if (!value) { pending.Clear(); StatusTitle.Text = "接收已停止"; StatusDetail.Text = "点击开始接收，即可再次连接移动设备。"; }
        SettingsHint.Text = value ? "音量、声音设备和高级混合可实时调整；连接设置在停止后修改。" : "";
        InstallDriverButton.IsEnabled = RestoreDriverButton.IsEnabled = !value && !driverBusy;
        SetPickerStates(); UpdateSetupState();
    }
    private void SetPickerStates()
    {
        if (OutputPicker == null) return;
        OutputPicker.IsEnabled = settings.mode != "native" && !pending.ContainsKey("output") && !driverBusy && !refreshingAudio;
        InputPicker.IsEnabled = settings.mode != "native" && !pending.ContainsKey("input") && !driverBusy && !refreshingAudio;
        MuteToggle.IsEnabled = settings.mode != "native" && !driverBusy;
        PlaybackGain.IsEnabled = MicrophoneGain.IsEnabled = settings.mode != "native" && !driverBusy;
        SystemGain.IsEnabled = settings.mode != "native" && !driverBusy;
        SystemGain.CanAdjust = settings.system_audio_enabled;
        SystemAudioToggle.IsEnabled = settings.mode != "native" && settings.calls && !driverBusy && systemAudioRequest == null;
        foreach (Control control in new Control[] { NameEntry, CallsToggle, MediaBufferPicker, CallBufferPicker, MaximumPicker, LdacToggle })
            control.IsEnabled = !running && !driverBusy && settings.mode != "native";
        VoiceCodecHint.Text = settings.mode == "native" ? VoiceCodecStatus : VoiceCodecStatus + "；用于通话及支持蓝牙麦克风的录音、录像应用";
        if (!running) SettingsHint.Text = settings.mode == "native" ? "共用蓝牙模式：设备名称、媒体编码、播放设备和音量由 Windows 管理，不提供蓝牙语音与麦克风。" : "";
        AdapterPicker.IsEnabled = settings.mode != "native" && !running && !driverBusy;
        UpdateDriverActions(); UpdateMixState(); UpdateMediaControls(); UpdateSetupState();
    }
    private bool VoiceBusy => voiceInUse || State.Devices.Any(card => card.CanAnswer || card.CanHangup);
    private void UpdateMediaControls()
    {
        if (CodecPicker == null || PlaybackModePicker == null) return;
        var dedicated = settings.mode != "native";
        CodecPicker.IsEnabled = dedicated && !driverBusy && restartWithCodec == null;
        PlaybackModePicker.IsEnabled = dedicated && !driverBusy && !mediaModePending && restartWithCodec == null;
        foreach (var card in State.Devices) card.CanSelectMedia = dedicated && settings.media_mode == "single" && State.Devices.Count >= 2;
        MediaModeHint.Text = !dedicated ? "此模式的媒体连接与编码由 Windows 管理。" : settings.media_mode == "mix"
            ? "一起听：各设备声音同时播放，可分别调节音量。" : "当前设备停止传音约 0.8 秒后自动接替等待播放的设备；点击设备名旁的标记可立即切换。语音使用中不自动切换媒体。";
        if (dedicated && State.Devices.Count >= 2) MediaModeHint.Text = $"已连接 {State.Devices.Count} 台 · " + MediaModeHint.Text;
        var desired = codecDraft ?? settings.codec;
        CodecHint.Text = !dedicated ? "此模式的媒体编码和码率由 Windows 管理，不提供蓝牙语音与麦克风。" : (desired switch
        {
            "auto" => "优先协商可用高清媒体编码；多设备断续时可切换 AAC。",
            "aac" => "AAC 最高 320 kb/s，兼顾音质与带宽。实际码率由移动设备协商。",
            "aac_192" => "AAC 最高 192 kb/s，减少无线带宽占用，适合多设备或信号不稳时使用。",
            "sbc" => "SBC 兼容性较好，实际码率由移动设备协商；不一定比 AAC 更低。",
            "ldac" => "LDAC 实验解码；并发更占带宽，建议仅一个设备出声。",
            _ => "高清媒体编码更占无线带宽，多设备同时播放可能丢包；建议只听一台。"
        }) + " 仅影响媒体播放，语音编码自动协商。";
        var dirty = codecDraft != null && codecDraft != settings.codec;
        CodecApplyRow.Visibility = dirty ? Visibility.Visible : Visibility.Collapsed;
        ApplyCodecButton.Content = running ? "应用并重连" : "保存媒体编码";
        ApplyCodecButton.IsEnabled = dedicated && !NeedsBluetoothSetup && !driverBusy && !VoiceBusy && !refreshingAudio && pending.Count == 0 && !mediaModePending && restartWithCodec == null;
        CodecApplyHint.Text = VoiceBusy ? "语音通道正在使用，请结束后再更换媒体编码。" : refreshingAudio || pending.Count != 0 || mediaModePending ? "正在更新声音设备或出声方式，请完成后再应用媒体编码。" : running
            ? "应用媒体编码会断开当前蓝牙连接；请随后重新连接移动设备。" : "保存媒体编码后，在下一次蓝牙连接时使用。";
        var high = settings.codec is "aptx" or "aptx_hd" or "ldac" || settings.codec == "auto" && highMediaActive;
        MultipointNotice.Visibility = dedicated && State.Devices.Count >= 2 && settings.media_mode == "mix" && mediaProtected ? Visibility.Visible : Visibility.Collapsed;
        MultipointText.Text = settings.media_mode == "single" ? $"已连接 {State.Devices.Count} 台 · 当前仅一个设备出声。需要一起听时，切换上方出声方式。"
            : mediaProtected ? "检测到持续丢包，已隔离不稳定一路的碎音。可降低码率后重连，或改为仅一个设备出声。"
            : high ? "已连接多台 · 高清并发可能断续。两路无法稳定播放时，切换 AAC 后重新连接。"
            : $"已连接 {State.Devices.Count} 台 · 当前同时出声。若出现断续，可进一步降低 AAC 码率。";
        LowerBitrateButton.Visibility = settings.media_mode == "mix" && settings.codec != "aac_192" ? Visibility.Visible : Visibility.Collapsed;
        LowerBitrateButton.Content = high ? "切换 AAC 并重连" : "降至 192 并重连";
        LowerBitrateButton.IsEnabled = !VoiceBusy && !driverBusy && !refreshingAudio && pending.Count == 0 && !mediaModePending && restartWithCodec == null;
    }
    private void PlaybackSettingsClick(object sender, RoutedEventArgs e) => ShowAdvancedAudio();
    private void CodecChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || CodecPicker?.SelectedItem is not ComboBoxItem selected) return;
        codecDraft = selected.Tag?.ToString(); UpdateMediaControls();
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
    private async void ApplyCodecClick(object sender, RoutedEventArgs e) => await ApplyCodecProfile();
    private async void LowerBitrateClick(object sender, RoutedEventArgs e) => await ApplyCodecProfile(settings.codec is "aptx" or "aptx_hd" or "ldac" || settings.codec == "auto" && highMediaActive ? "aac" : "aac_192");
    internal async Task ApplyCodecProfile(string? force = null)
    {
        if (VoiceBusy || driverBusy || refreshingAudio || pending.Count != 0 || mediaModePending || settings.mode == "native" || restartWithCodec != null) return;
        var next = JsonSerializer.Deserialize<EngineSettings>(JsonSerializer.Serialize(settings))!;
        next.codec = force ?? codecDraft ?? settings.codec;
        if (next.codec == "ldac") next.ldac_enabled = true;
        await FlushGains();
        if (running)
        {
            restartWithCodec = next; UpdateMediaControls();
            if (!await Send(new { command = "stop" })) { restartWithCodec = null; UpdateMediaControls(); }
        }
        else
        {
            if (await Send(new { command = "configure", settings = next }))
            { settings = next; codecDraft = null; ApplySettings(); FooterStatus.Text = "媒体编码已保存 · 下次连接时生效"; }
        }
    }
    private void UpdateMixState()
    {
        PlaybackGain.Secondary = (OutputPicker.SelectedItem as AudioChoice)?.Name ?? "默认播放设备（自动跟随）";
        MicrophoneGain.Secondary = settings.microphone_muted ? "发送到设备：已静音" : (InputPicker.SelectedItem as AudioChoice)?.Name ?? "默认麦克风";
        MutedIndicator.Visibility = settings.mode != "native" && settings.microphone_muted ? Visibility.Visible : Visibility.Collapsed;
        if (settings.mode == "native")
        { TransmitHint.Text = "共用蓝牙模式：媒体播放、编码和码率由 Windows 管理"; SystemAudioStatus.Text = "此模式不提供蓝牙麦克风"; return; }
        TransmitHint.Text = settings.microphone_muted ? "发送已暂停：麦克风与电脑声音均不发送" : "开启后停止发送麦克风和电脑声音，仍可听到移动设备。";
        SystemAudioStatus.Text = settings.system_audio_enabled ? systemAudioActive ? "电脑声音：正在发送到设备" : "电脑声音：使用蓝牙麦克风时加入" : "电脑声音：未加入";
        SystemGain.Secondary = systemAudioActive ? "正在发送到设备" : "与麦克风合并发送";
    }
    private async void StartClick(object sender, RoutedEventArgs e)
    {
        if (running) { StartButton.IsEnabled = false; await Send(new { command = "stop" }); return; }
        if (driverChecking || driverBusy || closing) return;
        if (!NeedsBluetoothSetup) await RefreshDrivers();
        if (NeedsBluetoothSetup) { SetupBluetoothClick(sender, e); return; }
        var selected = ReadSettings();
        if (!await Send(new { command = "start", settings = selected })) SetRunning(false);
    }
    private async Task SelectAudio(string direction, AudioChoice? choice)
    {
        if (applying || choice is null) return;
        if (choice.Index == (direction == "output" ? settings.output_device : settings.input_device)) return;
        var id = ++requestId; pending[direction] = id; SetPickerStates();
        if (!await Send(new { command = "audio_device", direction, device = choice.Index, endpoint_id = choice.EndpointId, request_id = id }))
        { pending.Remove(direction); ApplySettings(); }
    }
    private async void OutputChanged(object sender, SelectionChangedEventArgs e) => await SelectAudio("output", OutputPicker.SelectedItem as AudioChoice);
    private async void InputChanged(object sender, SelectionChangedEventArgs e) => await SelectAudio("input", InputPicker.SelectedItem as AudioChoice);
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
        if (applying) return;
        var id = ++requestId;
        systemAudioRequest = id;
        SystemAudioToggle.IsEnabled = false;
        if (!await Send(new { command = "system_audio", enabled = SystemAudioToggle.IsChecked == true, request_id = id }))
        { systemAudioRequest = null; ApplySettings(); }
    }
    private async void SaveSettingsClick(object sender, RoutedEventArgs e) => await SaveSettings();
    private async Task<bool> SaveSettings()
    {
        if (running) { ShowNotice("请先停止接收，再保存连接设置。"); return false; }
        if (!await Send(new { command = "configure", settings = ReadSettings() })) return false;
        preferences.Tray = TrayToggle.IsChecked == true; preferences.AutoStart = AutoStartToggle.IsChecked == true;
        if (!offline)
        {
            Directory.CreateDirectory(dataDirectory);
            File.WriteAllText(Path.Combine(dataDirectory, "desktop.json"), JsonSerializer.Serialize(preferences));
            using var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
            if (preferences.AutoStart) key.SetValue("BlueHead", $"{FrameworkCompat.QuoteArgument(FrameworkCompat.ProcessPath)} --tray"); else key.DeleteValue("BlueHead", false);
        }
        return true;
    }
    private void ModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (applying || ModePicker?.SelectedItem is not ComboBoxItem item) return;
        if (running || driverBusy) { applying = true; SelectTag(ModePicker, settings.mode); applying = false; return; }
        SetIntendedMode(item.Tag?.ToString() ?? "advanced");
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
    private async void SelectCallClick(object sender, RoutedEventArgs e) => await Act(sender, "select_call");
    private async void CallAudioClick(object sender, RoutedEventArgs e) => await Act(sender, "call_audio");
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
        DriverPicker.ItemsSource = inventory;
        DriverPicker.SelectedItem = inventory.FirstOrDefault(item => item.InstanceId == selected) ?? inventory.FirstOrDefault();
        UpdateDriverActions(); UpdateSetupState();
    }
    private void UpdateDriverActions()
    {
        var device = DriverPicker.SelectedItem as DriverDevice;
        var guide = BluetoothSetup.Adapter(device);
        var shared = settings.mode == "native";
        ModePicker.IsEnabled = !running && !driverBusy;
        DriverPicker.IsEnabled = !driverBusy && !driverChecking;
        InstallDriverButton.Visibility = shared ? Visibility.Collapsed : Visibility.Visible;
        InstallDriverButton.IsEnabled = !running && !driverBusy && !driverChecking && guide.CanInstall;
        RestoreDriverButton.IsEnabled = !running && !driverBusy && !driverChecking && guide.CanRestore;
        InstallDriverButton.Content = guide.InstallLabel;
        RestoreDriverButton.Content = guide.RestoreLabel;
        InstallDriverButton.ToolTip = RestoreDriverButton.ToolTip = running ? "先停止接收，再更换或恢复适配器。" : guide.Detail;
        DriverStateTitle.Text = guide.State;
        DriverStatus.Text = guide.Detail + (device?.HasBackup == true ? "\n原厂驱动备份已保留。" : device != null && !BluetoothSetup.Dedicated(device) ? "\n首次启用专用接收时会自动备份原厂驱动。" : "");
        DriverNativeDetails.Text = device == null ? "未检测到可配置的 USB 蓝牙适配器。" : $"驱动服务：{device.Service}\n驱动文件：{device.Inf}";
        DriverStepTitle.Text = shared ? "2  检查系统蓝牙" : "2  准备专用适配器";
        DriverStepDescription.Text = shared ? "适配器已专用时，先恢复给 Windows；系统蓝牙正常时可直接完成。" : "选择内置或外置蓝牙。启用会断开此适配器原有的连接，原厂驱动会自动备份。";
        DriverStopButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
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
        if (running || driverBusy) { ShowNotice("请先停止接收，再更换或恢复适配器。"); return; }
        var requested = (DriverPicker.SelectedItem as DriverDevice)?.InstanceId;
        if (requested == null) { ShowNotice("请选择一个蓝牙适配器。"); return; }
        await RefreshDrivers();
        if (running || driverBusy) return;
        if (driverInventoryError != null) { DriverResult.Text = driverInventoryError; return; }
        if (DriverPicker.SelectedItem is not DriverDevice device || !string.Equals(device.InstanceId, requested, StringComparison.OrdinalIgnoreCase))
        { DriverResult.Text = "所选适配器的连接已变化，请重新选择并检查状态。"; return; }
        var guide = BluetoothSetup.Adapter(device);
        if (operation == "install" ? settings.mode == "native" || !guide.CanInstall : !guide.CanRestore)
        { return; }
        if (offline) { SelfTestDriverOperations.Add(operation); return; }
        driverResultInstance = device.InstanceId;
        var description = operation == "install" ? "启用专用接收" : "恢复系统蓝牙";
        var detail = operation == "install" ? "将先备份原驱动，再将这个适配器交给 BlueHead。该适配器上的现有蓝牙连接会断开。" : "将停止使用专用驱动，把这个适配器恢复给 Windows 蓝牙。";
        if (MessageBox.Show(this, $"{device.Name}\n\n{detail}\n\n继续后 Windows 会请求管理员授权。", description, MessageBoxButton.OKCancel, MessageBoxImage.Information) != MessageBoxResult.OK) return;
        driverBusy = true; SetRunning(false); DriverProgress.Visibility = Visibility.Visible;
        DriverResult.Text = operation == "restore" ? "正在恢复驱动并初始化系统蓝牙，请保持适配器连接…" : "正在启用专用接收，请保持适配器连接…";
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "BlueHead.DriverHelper.exe");
            var start = new ProcessStartInfo(path) { UseShellExecute = true, Verb = "runas", WorkingDirectory = Path.GetDirectoryName(path), WindowStyle = ProcessWindowStyle.Hidden };
            start.AddArgument("--" + operation); start.AddArgument(device.InstanceId);
            using var helper = Process.Start(start) ?? throw new IOException("恢复工具未能启动。");
            await helper.WaitForExitAsync();
            var record = Drivers.ReadRecord(device.InstanceId);
            var success = helper.ExitCode is 0 or 3010;
            if (success) { ApplySuccessfulDriverOperation(operation, device); await Send(new { command = "configure", settings }); }
            DriverResult.Text = success ? (record?.RebootRequired == true ?
                operation == "restore" ? "驱动恢复完成，请重启电脑。" : "专用驱动已启用，请重启电脑。" :
                operation == "restore" ? "系统蓝牙已恢复，初始化检查通过。" : "专用接收已启用，开始接收时将自动初始化固件。") : record?.Error ?? "操作未完成，恢复工具中显示了原因。";
        }
        catch (Win32Exception error) when (error.NativeErrorCode == 1223) { DriverResult.Text = "管理员授权已取消，未开始更换。"; }
        catch (Exception error) { DriverResult.Text = error.Message; }
        finally
        {
            driverBusy = false; DriverProgress.Visibility = Visibility.Collapsed; SetRunning(false);
            await RefreshDrivers(); await Send(new { command = "inventory" });
        }
    }
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
    private async void InstallDriverClick(object sender, RoutedEventArgs e) => await DriverOperation("install");
    private async void RestoreDriverClick(object sender, RoutedEventArgs e) => await DriverOperation("restore");
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
    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (offline) return;
        if (driverBusy) { e.Cancel = true; ShowNotice("正在处理驱动，请等待操作完成。"); return; }
        if (preferences.Tray && !allowClose && !closing) { e.Cancel = true; Hide(); return; }
        if (closing) { e.Cancel = true; return; }
        e.Cancel = true; closing = true; timer.Stop(); StartButton.IsEnabled = false; FooterStatus.Text = "正在关闭蓝牙与麦克风…";
        await FlushGains(); await engine.ShutdownAsync(); engine.Dispose(); tray?.Dispose(); Closing -= OnClosing; Close();
    }
}
