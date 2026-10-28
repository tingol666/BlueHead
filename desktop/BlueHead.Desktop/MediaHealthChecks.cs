using System.IO;
using System.Text.Json;
using System.Windows;

namespace BlueHead.Desktop;

internal static partial class Program
{
    private static void MediaHealthChecks(MainWindow window, Dictionary<string, bool> checks, string folder, Action<string> receive)
    {
        double now = 0;
        MediaHealthTracker NewTracker() { now = 0; return new() { Clock = () => now }; }
        JsonElement Sample(long packets, long lost = 0, bool playing = true, string codec = "ldac", string protocol = "A2DP",
            long errors = 0, long dropped = 0, long concealed = 0, bool protectedAudio = false, double pps = 100) => JsonSerializer.SerializeToElement(new
            {
                codec, protocol, rate = codec is "aac" or "lc3" ? 48000 : 96000, channels = 2, depth = 0, packets,
                lost_packets = lost, decode_errors = errors, dropped_packets = dropped, malformed_packets = 0,
                expired_packets = 0, sync_errors = 0, errors, dropped, concealed_blocks = concealed,
                playing, packets_per_second = pps, selected = true, @protected = protectedAudio
            });
        void Bad(MediaHealthTracker tracker, int start = 0, int end = 5)
        { for (var i = start; i <= end; i++) { now = i; tracker.Observe("peer", Sample(i * 100, i * 5)); } }
        var tracker = NewTracker();
        for (var i = 0; i <= 12; i++) { now = i; tracker.Observe("peer", Sample(i * 100 + 50000, 5000)); }
        checks["media_health_uses_deltas_not_lifetime_loss_totals"] = !tracker.IsWarning("peer");
        tracker = NewTracker();
        Bad(tracker, end: 4);
        checks["media_health_waits_for_warmup_and_sustained_bad_windows"] = !tracker.IsWarning("peer");
        now = 5; tracker.Observe("peer", Sample(500, 25));
        checks["media_health_detects_real_sustained_packet_loss"] = tracker.IsWarning("peer");
        for (var i = 6; i <= 9; i++) { now = i; tracker.Observe("peer", Sample(i * 100, 25)); }
        checks["media_health_does_not_flicker_after_one_clean_window"] = tracker.IsWarning("peer");
        now = 10; tracker.Observe("peer", Sample(1000, 25));
        checks["media_health_clears_after_sustained_clean_recovery"] = !tracker.IsWarning("peer");
        tracker = NewTracker();
        for (var i = 0; i <= 15; i++) { now = i; tracker.Observe("peer", Sample(i * 100, i >= 3 ? 8 : 0)); }
        checks["media_health_ignores_isolated_short_loss_burst"] = !tracker.IsWarning("peer");
        tracker = NewTracker();
        for (var i = 0; i <= 10; i++) { now = i; tracker.Observe("peer", Sample(i * 10000, i * 5)); }
        checks["media_health_requires_meaningful_loss_density"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 6; tracker.Observe("peer", Sample(500, 25, playing: false, pps: 0));
        checks["media_health_pause_immediately_discards_old_warning"] = !tracker.IsWarning("peer") && tracker.Count == 0;
        now = 7; tracker.Observe("peer", Sample(600, 25));
        checks["media_health_resume_has_a_fresh_baseline"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 6; tracker.Observe("peer", Sample(500, 25));
        now = 7; tracker.Observe("peer", Sample(500, 25));
        checks["media_health_no_packet_progress_resets_warning"] = !tracker.IsWarning("peer") && tracker.Count == 0;
        tracker = NewTracker(); Bad(tracker);
        now = 6; tracker.Observe("peer", Sample(1, 0));
        checks["media_health_counter_rollback_is_a_new_stream"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 6; tracker.Observe("peer", Sample(600, 30, codec: "aac"));
        checks["media_health_codec_change_cannot_inherit_warning"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 6; tracker.Observe("peer", Sample(600, protocol: "LE Audio", codec: "lc3"));
        checks["media_health_transport_change_cannot_inherit_warning"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 15; tracker.Observe("peer", Sample(1500, 300));
        checks["media_health_long_telemetry_stall_does_not_score_a_giant_window"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 10;
        checks["media_health_stale_warning_expires_without_new_samples"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        now = 3; tracker.Observe("peer", Sample(700, 50));
        checks["media_health_clock_rollback_cannot_keep_old_warning"] = !tracker.IsWarning("peer");
        tracker = NewTracker(); Bad(tracker);
        tracker.SetContext("single");
        checks["media_health_playback_mode_change_resets_history"] = tracker.Count == 0;
        Bad(tracker);
        tracker.Retain(["other"]);
        checks["media_health_disconnected_peer_is_removed"] = tracker.Count == 0;
        tracker = NewTracker(); Bad(tracker);
        tracker.Observe("peer", Sample(600, 30), eligible: false);
        checks["media_health_voice_or_unselected_source_is_not_scored"] = tracker.Count == 0;
        tracker = NewTracker();
        for (var i = 0; i <= 5; i++)
        {
            now = i; tracker.Observe("peer", Sample(i * 100, i * 5)); tracker.Observe("healthy", Sample(i * 100));
        }
        checks["media_health_tracks_devices_independently"] = tracker.IsWarning("peer") && !tracker.IsWarning("healthy");
        tracker.Reset();
        checks["media_health_session_reset_clears_all_state"] = tracker.Count == 0;
        tracker = NewTracker();
        for (var i = 0; i <= 5; i++) { now = i; tracker.Observe("peer", Sample(i * 100, codec: "lc3", protocol: "LE Audio", concealed: i * 5)); }
        checks["media_health_uses_actual_le_loss_compensation_counters"] = tracker.IsWarning("peer") && tracker.HasLeWarning;
        tracker = NewTracker();
        for (var i = 0; i <= 5; i++) { now = i; tracker.Observe("peer", Sample(i * 100, errors: i * 4, dropped: i * 4)); }
        checks["media_health_detects_sustained_decode_and_queue_damage"] = tracker.IsWarning("peer");
        tracker = NewTracker();
        for (var i = 0; i < 200; i++) tracker.Observe("peer-" + i, Sample(100));
        checks["media_health_retains_bounded_number_of_streams"] = tracker.Count == MediaHealthTracker.MaximumStreams;
        tracker = NewTracker();
        foreach (var malformed in new[] { "{}", "null", "{\"codec\":\"ldac\",\"playing\":true,\"packets\":\"100\"}", "{\"codec\":\"ldac\",\"playing\":true,\"packets\":-1}" })
        { using var document = JsonDocument.Parse(malformed); tracker.Observe("peer", document.RootElement); }
        checks["media_health_missing_or_malformed_counters_never_create_warning"] = tracker.Count == 0;

        var clock = window.MediaHealth.Clock;
        var playback = (window.PlaybackModePicker.SelectedItem as System.Windows.Controls.ComboBoxItem)?.Tag?.ToString() ?? "mix";
        void Event(object value) => receive(JsonSerializer.Serialize(value));
        void Telemetry(JsonElement media, bool two = false)
        {
            var rows = new[] { new { key = "health-primary", media, call = new { active = false } } }.ToList();
            if (two) rows.Add(new { key = "health-other", media = Sample((long)now * 100), call = new { active = false } });
            Event(new { protocol = 1, @event = "telemetry", running = true, audio = new { }, devices = rows });
        }
        try
        {
            Event(new { @event = "stopped" });
            window.MediaHealth.Clock = () => now;
            Event(new { @event = "media_mode", success = true, mode = "single" });
            Event(new { @event = "starting" }); window.ShowPage("home");
            var commands = window.SelfTestCommands.Count;
            for (var i = 0; i <= 5; i++) { now = i; Telemetry(Sample(i * 100, i * 5)); }
            checks["single_device_sustained_loss_has_compact_visible_guidance"] = window.MultipointNotice.IsVisible && window.MultipointText.Text.Contains("持续不稳") && window.MultipointText.Text.Contains("AAC 小齿轮降低码率") && window.MultipointText.Text.Contains("设备蓝牙设置") && window.MultipointText.Text.Contains("重连");
            checks["media_warning_never_downgrades_codec_or_changes_connections"] = window.SelfTestCommands.Count == commands && window.State.Devices.Single().MediaDescription.Contains("LDAC");
            Capture(window, Path.Combine(folder, "home-single-device-instability.png"));
            window.LowerBitrateButton.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent)); Pump();
            checks["media_warning_action_opens_existing_global_codec_settings"] = window.PolicyWindow?.IsVisible == true;
            window.PolicyWindow?.Close(); Pump();
            now = 6; Telemetry(Sample(500, 25, playing: false, pps: 0));
            checks["paused_media_hides_sustained_loss_notice"] = window.MultipointNotice.Visibility == Visibility.Collapsed;
            Event(new { @event = "media_mode", success = true, mode = "mix" });
            for (var i = 0; i <= 5; i++) { now = 7 + i; Telemetry(Sample(i * 100, i * 5), two: true); }
            checks["multiple_device_sustained_loss_keeps_same_compact_action"] = window.MultipointNotice.IsVisible && window.State.Devices.Count == 2 && window.LowerBitrateButton.IsVisible;
            Event(new { @event = "device", key = "health-primary", state = "已断开" });
            checks["disconnecting_damaged_source_clears_notice_for_healthy_peer"] = window.MultipointNotice.Visibility == Visibility.Collapsed;
            now = 13; Telemetry(Sample(600, 30, protectedAudio: true));
            checks["native_protection_stays_immediate_with_explicit_reconnect_help"] = window.MultipointNotice.IsVisible && window.State.Devices.Single().PlaybackNotice.Contains("隔离") && window.State.Devices.Single().PlaybackNotice.Contains("齿轮") && window.State.Devices.Single().PlaybackNotice.Contains("重连");
            Event(new { @event = "stopped" });
            checks["stopping_receiver_clears_media_warning_state"] = window.MediaHealth.Count == 0 && window.MultipointNotice.Visibility == Visibility.Collapsed;
        }
        finally
        {
            window.PolicyWindow?.Close();
            Event(new { @event = "stopped" });
            Event(new { @event = "media_mode", success = true, mode = playback });
            window.MediaHealth.Clock = clock;
            window.MediaHealth.Reset();
        }
    }
}
