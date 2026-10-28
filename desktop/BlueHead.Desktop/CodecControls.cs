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
    private readonly Dictionary<string, DeviceCodecWindow> codecWindows = [];
    internal CodecPolicyWindow? PolicyWindow { get; private set; }
    internal DeviceCodecWindow? DeviceCodecFor(string key) => codecWindows.GetValueOrDefault(key);
    private void InitializeCodecControls()
    {
        settingsTimer.Tick += async (_, _) => await FlushSettings();
        NameEntry.TextChanged += SettingsEdited;
        foreach (var toggle in new[] { CallsToggle, VoiceProtectionToggle, LdacToggle, Lhdc5Toggle, LeAudioToggle, TrayToggle, AutoStartToggle })
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
        if (ReferenceEquals(sender, LdacToggle) || ReferenceEquals(sender, Lhdc5Toggle))
        {
            var allowed = CodecPolicyWindow.Allowed(settings);
            var code = ReferenceEquals(sender, LdacToggle) ? "ldac" : "lhdc5";
            if (((CheckBox)sender).IsChecked == true) allowed.Add(code); else allowed.Remove(code);
            settings.allowed_codecs = allowed.ToList();
            if (settings.codec != "auto" && !allowed.Contains(CodecPolicyWindow.Canonical(settings.codec))) settings.codec = "auto";
            var previous = applying; applying = true; SelectTag(CodecPicker, settings.codec); applying = previous;
        }
        preferences.Tray = TrayToggle.IsChecked == true; preferences.AutoStart = AutoStartToggle.IsChecked == true;
        ReadSettings();
        QueueSettings();
    }
    private void QueueSettings()
    {
        settingsDirty = true; settingsTimer.Stop(); settingsTimer.Start();
        SettingsHint.Text = "正在自动保存…";
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
            next.maximum_devices, next.codec, next.allowed_codecs, next.ldac_enabled, next.lhdc5_enabled,
            next.connection_policy, next.le_audio_enabled };
        preferencesRequest = ++requestId;
        preferencesAcknowledged = false;
        if (!await Send(new { command = "preferences", request_id = preferencesRequest, values }))
        { settingsDirty = true; SettingsHint.Text = "设置尚未保存，后台恢复后会再次保存。"; return false; }
        try { SaveDesktopPreferences(); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        { SettingsHint.Text = "连接偏好已发送；窗口／自启动设置保存失败：" + error.Message; return false; }
        if (offline) SettingsHint.Text = running ? "已自动保存。连接偏好下次开始接收生效；经典编码用于下次设备连接，现有连接继续播放。" : "已自动保存。";
        else if (!preferencesAcknowledged) SettingsHint.Text = "正在自动保存…";
        UpdateMediaControls(); UpdateProtocolAvailability(); RefreshOpenHelp();
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
            item.IsEnabled = code == "auto" || allowed.Contains(code) && !unavailableCodecs.ContainsKey(code);
            item.Opacity = item.IsEnabled ? 1 : .55;
            item.ToolTip = unavailableCodecs.TryGetValue(code, out var reason) ? reason : !allowed.Contains(code) && code != "auto" ? "尚未允许此编码，点击媒体编码右侧的设置按钮启用。" : null;
        }
    }
    internal void OpenCodecPolicy()
    {
        if (settings.mode == "native" || driverBusy) return;
        if (PolicyWindow != null) { PolicyWindow.Activate(); return; }
        PolicyWindow = new(settings, unavailableCodecs, (policy, allowed, preferred) =>
        {
            settings.connection_policy = policy; settings.le_audio_enabled = policy == "prefer_le";
            settings.allowed_codecs = allowed; settings.codec = preferred;
            settings.ldac_enabled = allowed.Contains("ldac"); settings.lhdc5_enabled = allowed.Contains("lhdc5");
            ApplySettings(); QueueSettings();
        }) { Owner = this };
        PolicyWindow.Closed += async (_, _) => { PolicyWindow = null; await FlushSettings(); };
        PolicyWindow.Show();
    }
    private void CodecSettingsClick(object sender, RoutedEventArgs e) => OpenCodecPolicy();
    internal void OpenDeviceCodec(DeviceCard card)
    {
        if (!devices.TryGetValue(card.Key, out var current) || !ReferenceEquals(card, current) || settings.mode == "native") return;
        if (codecWindows.TryGetValue(card.Key, out var previous)) previous.Close();
        var window = new DeviceCodecWindow(card.Key, card.Name, Send, () => ++requestId) { Owner = this };
        codecWindows[card.Key] = window;
        window.Closed += (_, _) => { if (codecWindows.GetValueOrDefault(card.Key) == window) codecWindows.Remove(card.Key); };
        window.Show();
    }
    private void DeviceCodecClick(object sender, RoutedEventArgs e) { if (Card(sender) is { } card) OpenDeviceCodec(card); }
    private bool ApplyCodecMessage(JsonElement message)
    {
        var kind = message.Text("event");
        if (kind == "preferences")
        {
            if (message.Number("request_id") == preferencesRequest)
            {
                preferencesAcknowledged = true;
                if (!settingsDirty) SettingsHint.Text = message.Text("message");
                if (!message.Flag("success")) settingsDirty = true;
                else FooterStatus.Text = "设置已保存 · 现有连接保持";
            }
            return true;
        }
        if (kind == "codec_capabilities")
        {
            unavailableCodecs.Clear();
            foreach (var row in message.GetProperty("codecs").EnumerateArray()) if (!row.Flag("available")) unavailableCodecs[row.Text("codec")] = row.Text("reason", "当前解码器不可用");
            RefreshCodecChoices(); return true;
        }
        if (kind is not ("codec_options" or "codec_switch" or "codec_state" or "le_audio_discovery")) return false;
        if (message.Flag("stale")) return true;
        var key = message.Text("key");
        if (codecWindows.TryGetValue(key, out var window)) window.Receive(message);
        if (devices.TryGetValue(key, out var card))
        {
            if (kind == "codec_state") card.CodecNote = message.Text("reason");
            if (kind == "le_audio_discovery") card.CodecNote = message.Text("phase") == "streaming" ? "" : message.Text("reason");
            if (kind == "codec_switch") { card.CodecNote = message.Text("message"); FooterStatus.Text = card.CodecNote; }
        }
        return true;
    }
    private void EndCodecConnections() { foreach (var window in codecWindows.Values) window.Disconnected(); }
}
