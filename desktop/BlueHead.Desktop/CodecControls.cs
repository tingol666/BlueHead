using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using BlueHead.Platform;
using Microsoft.Win32;

namespace BlueHead.Desktop;

public partial class MainWindow
{
    private readonly DispatcherTimer settingsTimer = new() { Interval = TimeSpan.FromMilliseconds(300) };
    private bool settingsDirty;
    private int preferencesRequest;
    private bool preferencesAcknowledged;
    private readonly Dictionary<string, string> unavailableCodecs = [];
    private readonly HashSet<string> codecReconnectDevices = [];
    private sealed record PreferenceContext(string[] Devices, int Session);
    private readonly Dictionary<int, PreferenceContext> pendingPreferenceReplies = [];
    private int codecSession;
    private string? activeStartupSettings;
    private bool codecRestartRequired;
    private string codecSaveError = "";
    internal CodecPolicyWindow? PolicyWindow { get; private set; }
    private void InitializeCodecControls()
    {
        settingsTimer.Tick += async (_, _) => await FlushSettings();
        NameEntry.TextChanged += SettingsEdited;
        foreach (var toggle in new[] { CallsToggle, VoiceProtectionToggle, LeAudioToggle, TrayToggle, AutoStartToggle })
        { toggle.Checked += SettingsEdited; toggle.Unchecked += SettingsEdited; }
        foreach (var picker in new[] { MediaBufferPicker, CallBufferPicker, MaximumPicker, ConnectionPolicyPicker }) picker.SelectionChanged += SettingsEdited;
    }
    private void SetPolicyControls(string policy)
    {
        var previous = applying; applying = true;
        LeAudioToggle.IsChecked = policy == "prefer_le"; SelectTag(ConnectionPolicyPicker, policy);
        applying = previous;
    }
    private void SettingsEdited(object sender, RoutedEventArgs e)
    {
        if (applying || closing) return;
        if (ReferenceEquals(sender, LeAudioToggle)) SetPolicyControls(LeAudioToggle.IsChecked == true ? "prefer_le" : "classic");
        if (ReferenceEquals(sender, ConnectionPolicyPicker)) SetPolicyControls((ConnectionPolicyPicker.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "classic");
        preferences.Tray = TrayToggle.IsChecked == true; preferences.AutoStart = AutoStartToggle.IsChecked == true;
        ReadSettings();
        QueueSettings();
    }
    private void QueueSettings()
    {
        settingsDirty = true; settingsTimer.Stop(); settingsTimer.Start();
        PolicyWindow?.Synchronize(settings);
        codecSaveError = "";
        SettingsHint.Text = "正在自动保存…";
        UpdateCodecNotice();
    }
    internal async Task<bool> FlushSettings()
    {
        settingsTimer.Stop();
        if (!settingsDirty) return true;
        settingsDirty = false;
        var next = ReadSettings();
        if (next.name.Length == 0)
        { settingsDirty = true; SettingsHint.Text = "请填写电脑对外显示的名称，随后会自动保存。"; return false; }
        if (System.Text.Encoding.UTF8.GetByteCount(next.name) > 200)
        { settingsDirty = true; SettingsHint.Text = "电脑名称过长，请缩短后会自动保存。"; return false; }
        var values = new { next.name, next.calls, next.voice_single_device, next.buffer_ms, next.call_buffer_ms,
            next.maximum_devices, next.codec, next.allowed_codecs, next.codec_parameters, next.ldac_enabled, next.lhdc5_enabled,
            next.connection_policy, next.le_audio_enabled };
        preferencesRequest = ++requestId;
        preferencesAcknowledged = false;
        pendingPreferenceReplies[preferencesRequest] = new(devices.Keys.ToArray(), codecSession);
        if (pendingPreferenceReplies.Count > 128) pendingPreferenceReplies.Remove(pendingPreferenceReplies.Keys.Min());
        if (!await Send(new { command = "preferences", request_id = preferencesRequest, values }))
        { pendingPreferenceReplies.Remove(preferencesRequest); settingsDirty = true; SettingsHint.Text = "设置尚未保存，后台恢复后会再次保存。"; return false; }
        try { SaveDesktopPreferences(); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        { SettingsHint.Text = "连接偏好已发送；窗口／自启动设置保存失败：" + error.Message; return false; }
        if (offline) { preferencesAcknowledged = true; SettingsHint.Text = "已自动保存。编码和参数需在发送设备上断开 BlueHead 后重新连接。"; }
        else if (!preferencesAcknowledged) SettingsHint.Text = "正在自动保存…";
        UpdateMediaControls(); UpdateProtocolAvailability(); RefreshOpenHelp();
        UpdateCodecNotice();
        return true;
    }
    private void SaveDesktopPreferences()
    {
        preferences.Tray = TrayToggle.IsChecked == true; preferences.AutoStart = AutoStartToggle.IsChecked == true;
        if (offline) return;
        Directory.CreateDirectory(dataDirectory);
        File.WriteAllText(Path.Combine(dataDirectory, "desktop.json"), JsonSerializer.Serialize(preferences));
        if (!string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("BLUEHEAD_SOURCE_ROOT"))) return;
        using var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
        if (preferences.AutoStart) key.SetValue("BlueHead", $"{FrameworkCompat.QuoteArgument(FrameworkCompat.ProcessPath)} --tray"); else key.DeleteValue("BlueHead", false);
    }
    private void RefreshCodecChoices()
    {
        var allowed = CodecPolicyWindow.Allowed(settings);
        foreach (var item in CodecPicker.Items.OfType<ComboBoxItem>())
        {
            var code = CodecPolicyWindow.Canonical(item.Tag?.ToString() ?? "auto");
            item.Content = CodecBitrateLimits.Label(code);
            item.IsEnabled = code == "auto" || allowed.Contains(code) && !unavailableCodecs.ContainsKey(code);
            item.Opacity = item.IsEnabled ? 1 : .55;
            var availability = unavailableCodecs.TryGetValue(code, out var reason) ? reason : !allowed.Contains(code) && code != "auto" ? "尚未允许此编码，点击媒体编码右侧的设置按钮启用。" : "";
            var font = new System.Windows.Media.FontFamily("Segoe UI, Microsoft YaHei UI");
            item.ToolTip = availability.Length > 0 ? new ToolTip { FontFamily = font, Content = new TextBlock { FontFamily = font, MaxWidth = 340,
                TextWrapping = TextWrapping.Wrap, Text = availability } } : null;
            ToolTipService.SetShowOnDisabled(item, true);
        }
    }
    internal void OpenCodecPolicy()
    {
        if (settings.mode == "native" || driverBusy) return;
        if (PolicyWindow != null) { PolicyWindow.Activate(); return; }
        PolicyWindow = new(settings, unavailableCodecs, (policy, allowed, preferred, parameters) =>
        {
            settings.connection_policy = policy; settings.le_audio_enabled = policy == "prefer_le";
            settings.allowed_codecs = allowed; settings.codec = preferred;
            settings.codec_parameters = parameters;
            settings.ldac_enabled = allowed.Contains("ldac"); settings.lhdc5_enabled = allowed.Contains("lhdc5");
            ApplySettings(); QueueSettings();
        }) { Owner = this };
        PolicyWindow.Closed += async (_, _) => { PolicyWindow = null; await FlushSettings(); };
        PolicyWindow.Show();
        UpdateCodecNotice();
    }
    private void CodecSettingsClick(object sender, RoutedEventArgs e) => OpenCodecPolicy();
    private bool ApplyCodecMessage(JsonElement message)
    {
        var kind = message.Text("event");
        if (kind == "preferences")
        {
            var reply = message.Number("request_id");
            if (reply < 1 || reply > int.MaxValue || reply != Math.Truncate(reply)) return true;
            pendingPreferenceReplies.TryGetValue((int)reply, out var context);
            pendingPreferenceReplies.Remove((int)reply);
            if (message.Flag("success") && running && context is not null && context.Session == codecSession)
            {
                if (message.Flag("reconnect_required")) codecReconnectDevices.UnionWith(context.Devices.Where(devices.ContainsKey));
                if (message.Flag("restart_required")) codecRestartRequired = true;
            }
            if (message.Number("request_id") == preferencesRequest)
            {
                preferencesAcknowledged = true;
                if (!settingsDirty) SettingsHint.Text = message.Text("message");
                if (!message.Flag("success")) { settingsDirty = true; codecSaveError = message.Text("message"); }
                else
                {
                    codecSaveError = "";
                    FooterStatus.Text = "设置已保存";
                }
            }
            UpdateCodecNotice();
            return true;
        }
        if (kind == "codec_capabilities")
        {
            unavailableCodecs.Clear();
            if (message.TryGetProperty("codecs", out var rows) && rows.ValueKind == JsonValueKind.Array)
                foreach (var row in rows.EnumerateArray()) if (row.ValueKind == JsonValueKind.Object && !row.Flag("available")) unavailableCodecs[row.Text("codec")] = row.Text("reason", "当前解码器不可用");
            RefreshCodecChoices(); PolicyWindow?.RefreshAvailability(); return true;
        }
        if (kind is "codec_options" or "codec_switch") return true;
        if (kind is not ("codec_state" or "le_audio_discovery")) return false;
        if (message.Flag("stale")) return true;
        var key = message.Text("key");
        if (devices.TryGetValue(key, out var card))
        {
            if (kind == "codec_state") card.CodecNote = message.Text("reason");
            if (kind == "le_audio_discovery") card.CodecNote = message.Text("phase") == "streaming" ? "" : message.Text("reason");
        }
        return true;
    }
    private string StartupSettings() => JsonSerializer.Serialize(new { settings.name, settings.calls, settings.voice_single_device, settings.buffer_ms, settings.call_buffer_ms, settings.maximum_devices, policy = settings.le_audio_enabled ? "prefer_le" : "classic" });
    private void StartCodecSession() { codecSession++; codecReconnectDevices.Clear(); codecRestartRequired = false; activeStartupSettings = StartupSettings(); }
    private void ResetCodecPending() { codecSession++; codecReconnectDevices.Clear(); codecRestartRequired = false; activeStartupSettings = null; pendingPreferenceReplies.Clear(); UpdateCodecNotice(); }
    private void UpdateCodecNotice()
    {
        if (CodecReconnectNotice == null) return;
        if (codecRestartRequired && activeStartupSettings == StartupSettings()) codecRestartRequired = false;
        codecReconnectDevices.RemoveWhere(key => !devices.ContainsKey(key));
        var reminder = codecRestartRequired ? "连接设置已保存。请先点右上角“停止接收”，再点“开始接收”，然后重新连接设备。"
            : codecReconnectDevices.Count > 0 ? "编码设置已保存。请在发送设备的蓝牙设置中断开 BlueHead，再重新连接；当前连接仍用原格式。" : "";
        CodecReconnectNotice.Visibility = running && settings.mode != "native" && reminder.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        CodecReconnectText.Text = reminder;
        if (PolicyWindow != null)
        {
            PolicyWindow.SaveState.Text = codecSaveError.Length > 0 ? codecSaveError
                : settingsDirty || preferencesRequest > 0 && !preferencesAcknowledged ? "正在自动保存…"
                : reminder.Length > 0 ? reminder
                : preferencesRequest > 0 ? "已保存。编码参数在下次设备连接生效；连接偏好在下次开始接收生效。"
                : "修改自动保存。编码和参数需断开并重新连接设备；连接偏好需停止接收，再开始接收。";
        }
    }
}
