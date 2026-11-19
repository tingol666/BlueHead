using BlueHead.Platform;
using System.IO;

namespace BlueHead.Desktop;

internal static partial class Program
{
    private sealed class AudioSwitchPort(DriverRecord record) : IDriverAudioQuiescence
    {
        public bool SoleWindowsRadio { get; set; } = true;
        public bool GatewayRunning { get; set; } = true;
        public List<string> Active { get; } = ["voice", "media"];
        public HashSet<string> Absent { get; } = [];
        public HashSet<string> ResumeFailures { get; } = [];
        public List<string> Calls { get; } = [];
        public bool StopFailure, SuspendFailure, SuspendDeclined, StartFailure;
        public bool JournalPrecedesMutation { get; private set; } = true;
        public IReadOnlyList<string> ActiveAudioDevices() => Active;
        public void Save() => Calls.Add("save");
        public void StopGateway()
        {
            JournalPrecedesMutation &= record.AudioGatewayWasRunning;
            Calls.Add("stop_gateway");
            if (StopFailure) throw new IOException("service busy");
            GatewayRunning = false;
        }
        public void StartGateway()
        {
            Calls.Add("start_gateway");
            if (StartFailure) throw new IOException("service delayed");
            GatewayRunning = true;
        }
        public bool Suspend(string instance)
        {
            JournalPrecedesMutation &= record.SuspendedAudioDevices.Contains(instance);
            Calls.Add("suspend:" + instance);
            if (SuspendFailure && instance == "media") throw new IOException("audio busy");
            return !SuspendDeclined;
        }
        public bool Resume(string instance)
        {
            Calls.Add("resume:" + instance);
            if (ResumeFailures.Contains(instance)) throw new IOException("resume delayed");
            return !Absent.Contains(instance);
        }
    }
    private static void DriverAudioSwitchChecks(Dictionary<string, bool> checks)
    {
        var record = new DriverRecord(); var port = new AudioSwitchPort(record);
        var lease = new DriverAudioQuiescence(record, port);
        checks["driver_audio_journals_before_service_and_device_mutation"] = port.JournalPrecedesMutation;
        checks["driver_audio_is_retired_before_binding"] = !port.GatewayRunning && record.SuspendedAudioDevices.Count == 2;
        lease.Dispose(); var count = port.Calls.Count; lease.Dispose();
        checks["driver_audio_finally_restores_exact_devices_and_previous_service_state"] = port.GatewayRunning && record.SuspendedAudioDevices.Count == 0 && !record.AudioGatewayWasRunning;
        checks["driver_audio_disposal_is_idempotent"] = port.Calls.Count == count;

        record = new(); port = new(record) { SoleWindowsRadio = false };
        using (new DriverAudioQuiescence(record, port)) { }
        checks["multiple_windows_radios_never_stop_global_voice_service"] = !port.Calls.Contains("stop_gateway") && !port.Calls.Contains("start_gateway") && port.Calls.Contains("suspend:voice");

        record = new(); port = new(record) { GatewayRunning = false };
        using (new DriverAudioQuiescence(record, port)) { }
        checks["originally_stopped_voice_service_is_not_started"] = !port.GatewayRunning && !port.Calls.Contains("start_gateway");

        record = new(); port = new(record) { StopFailure = true };
        try { using var ignored = new DriverAudioQuiescence(record, port); } catch (IOException) { }
        checks["failed_voice_release_compensates_without_leaving_service_stopped"] = port.GatewayRunning && !record.AudioGatewayWasRunning && !port.Calls.Any(call => call.StartsWith("suspend:"));

        record = new(); port = new(record) { SuspendFailure = true };
        try { using var ignored = new DriverAudioQuiescence(record, port); } catch (IOException) { }
        checks["partial_audio_retirement_restores_prior_changes_on_failure"] = port.Calls.Contains("resume:voice") && port.Calls.Contains("resume:media") && port.GatewayRunning && record.SuspendedAudioDevices.Count == 0;

        record = new(); port = new(record) { SuspendDeclined = true };
        using (new DriverAudioQuiescence(record, port)) { }
        checks["declined_audio_disable_never_manufactures_a_restore_obligation"] = !port.Calls.Any(call => call.StartsWith("resume:")) && record.SuspendedAudioDevices.Count == 0;

        record = new(); port = new(record); port.Absent.Add("voice");
        using (new DriverAudioQuiescence(record, port)) { }
        checks["absent_audio_child_keeps_its_restore_journal_for_windows_return"] = record.SuspendedAudioDevices.SequenceEqual(new[] { "voice" });
        port.Active.Clear(); port.Absent.Clear();
        using (new DriverAudioQuiescence(record, port)) { }
        checks["returning_windows_child_is_reenabled_and_journal_retired"] = record.SuspendedAudioDevices.Count == 0;

        record = new() { AudioGatewayWasRunning = true, SuspendedAudioDevices = ["interrupted"] };
        port = new(record) { GatewayRunning = false, SoleWindowsRadio = false };
        using (new DriverAudioQuiescence(record, port)) { }
        checks["interrupted_helper_recovers_owned_state_before_next_operation"] = port.Calls.IndexOf("resume:interrupted") < port.Calls.IndexOf("suspend:voice") && port.GatewayRunning && !record.AudioGatewayWasRunning;

        record = new(); port = new(record); port.ResumeFailures.Add("voice");
        using (new DriverAudioQuiescence(record, port)) { }
        checks["one_child_restore_failure_does_not_skip_other_children_or_service"] = port.Calls.Contains("resume:media") && port.GatewayRunning && record.SuspendedAudioDevices.SequenceEqual(new[] { "voice" }) && record.AudioRestoreWarning != null;

        record = new(); port = new(record);
        using (new DriverAudioQuiescence(record, port)) { port.StartFailure = true; }
        checks["delayed_service_restore_stays_in_durable_journal"] = record.AudioGatewayWasRunning && record.AudioRestoreWarning != null;
        port.StartFailure = false; port.SoleWindowsRadio = false; port.Active.Clear();
        using (new DriverAudioQuiescence(record, port)) { }
        checks["later_driver_operation_retries_pending_service_restore"] = port.GatewayRunning && !record.AudioGatewayWasRunning && record.AudioRestoreWarning == null;

        record = new(); port = new(record); port.Active.Clear(); port.Active.AddRange(["sideband", "voice", "media"]);
        using (new DriverAudioQuiescence(record, port)) { }
        checks["sideband_consumer_is_restored_after_its_voice_targets"] =
            port.Calls.IndexOf("resume:media") < port.Calls.IndexOf("resume:voice") &&
            port.Calls.IndexOf("resume:voice") < port.Calls.IndexOf("resume:sideband") &&
            port.Calls.IndexOf("resume:sideband") < port.Calls.IndexOf("start_gateway");

        string hardware = @"INTELAUDIO\CTLR_DEV_1234&LINKTYPE_03&DEVTYPE_00&VEN_8086&DEV_AE30";
        var media = DriverAudioDevicePolicy.MediaClass;
        checks["verified_bluetooth_only_sideband_is_eligible_on_sole_radio"] = DriverAudioDevicePolicy.CanRetireSideband(true, true, media, "IntcBTAu", hardware);
        checks["sideband_retirement_is_not_tied_to_a_radio_or_pc_model"] = DriverAudioDevicePolicy.CanRetireSideband(true, true, media, "intcbtau", hardware.Replace("1234", "FFFF"));
        checks["sideband_retirement_is_forbidden_with_other_windows_radio"] = !DriverAudioDevicePolicy.CanRetireSideband(false, true, media, "IntcBTAu", hardware);
        checks["sideband_retirement_requires_selected_bypass_profile"] = !DriverAudioDevicePolicy.CanRetireSideband(true, false, media, "IntcBTAu", hardware);
        checks["sideband_policy_rejects_microphone_link"] = !DriverAudioDevicePolicy.CanRetireSideband(true, true, media, "IntcBTAu", hardware.Replace("LINKTYPE_03", "LINKTYPE_02"));
        checks["sideband_policy_never_disables_audio_controller_or_normal_codec"] = !DriverAudioDevicePolicy.BluetoothOnlySideband(media, "IntcOED", hardware) && !DriverAudioDevicePolicy.BluetoothOnlySideband(media, "RTKVHD64", hardware);
        checks["sideband_policy_requires_device_class_and_bus_identity"] = !DriverAudioDevicePolicy.BluetoothOnlySideband(Guid.Empty, "IntcBTAu", hardware) && !DriverAudioDevicePolicy.BluetoothOnlySideband(media, "IntcBTAu", hardware.Replace("INTELAUDIO", "USB"));

        var feedback = BluetoothSetup.DriverCompletion("install", 3010, new DriverRecord { Stage="winusb_restart_required", RebootRequired=true, ReleaseDiagnostic="通话连接占用" });
        checks["remaining_voice_veto_explains_cause_attempt_and_next_button"] = !feedback.Ready && feedback.Message.Contains("仍占用") && feedback.Message.Contains("已尝试") && feedback.Message.Contains("重新检测");
    }
}
