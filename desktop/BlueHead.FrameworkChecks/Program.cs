using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using BlueHead.Platform;
using BlueHead.Desktop;

internal static class Program
{
    private static readonly Dictionary<string, bool> checks = new();
    private static int Main(string[] arguments)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (arguments.Length > 0 && arguments[0] == "--echo") { Console.WriteLine(JsonSerializer.Serialize(arguments.Skip(1).ToArray())); return 0; }
        if (arguments.Length > 0 && arguments[0] == "--child") { Console.WriteLine(Process.GetCurrentProcess().Id); Console.Out.Flush(); Thread.Sleep(30000); return 0; }
        if (arguments.Length > 0 && arguments[0] == "--parent-child")
        {
            Console.ReadLine();
            using var child = Start("--child"); Console.WriteLine(child.Id); Console.Out.Flush(); Thread.Sleep(30000); return 0;
        }
        if (arguments.Length > 0 && arguments[0] == "--sleep") { Thread.Sleep(150); return 0; }
        var root = arguments.Length > 0 ? Path.GetFullPath(arguments[0]) : Path.Combine(AppContext.BaseDirectory, "results");
        Directory.CreateDirectory(root);
        try { Run(root).GetAwaiter().GetResult(); }
        catch (Exception error) { checks["unhandled_exception"] = false; File.WriteAllText(Path.Combine(root, "framework-error.txt"), error.ToString()); }
        var passed = checks.Values.All(value => value);
        File.WriteAllText(Path.Combine(root, "framework-checks.json"), JsonSerializer.Serialize(new { passed, checks, scope = "Framework compatibility; temporary own files and processes only; no radio, audio, or driver changes" }, new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine($"Framework checks: {checks.Count}, passed: {passed}");
        return passed ? 0 : 1;
    }
    private static Process Start(params string[] arguments)
    {
        var start = new ProcessStartInfo(FrameworkCompat.ProcessPath) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true, RedirectStandardOutput = true, StandardOutputEncoding = Encoding.UTF8 };
        foreach (var argument in arguments) start.AddArgument(argument);
        return Process.Start(start) ?? throw new IOException("Test child failed to start.");
    }
    private static async Task Run(string root)
    {
        checks["x64_process"] = Environment.Is64BitProcess && IntPtr.Size == 8;
        FirmwareChecks.Run(root, checks);
        HandoffChecks.Run(checks);
        var restartAdapter = new DriverDevice(@"USB\VID_8087&PID_0033\offline", "offline", "WinUSB", "offline.inf", "", false, false) { RebootRequired = true };
        checks["restart_matches_selected_adapter"] = Drivers.RequiresRestart("usb:8087:0033#0", [restartAdapter]) && !Drivers.RequiresRestart("usb:1111:2222#0", [restartAdapter]);
        checks["duplicate_usb_models_do_not_guess_instance_order"] = !Drivers.RequiresRestart("usb:8087:0033#0", [restartAdapter, restartAdapter with { InstanceId = @"USB\VID_8087&PID_0033\another", RebootRequired = false }]);
        checks["driver_restart_state_roundtrips"] = JsonSerializer.Deserialize<DriverDevice>(JsonSerializer.Serialize(restartAdapter))?.RebootRequired == true && restartAdapter.DisplayName.Contains("需要重启 Windows");
        var dedicated = restartAdapter with { RebootRequired = false, Healthy = true, HasBackup = true };
        var system = dedicated with { Service = "ibtusb", SystemRadioReady = true };
        var other = dedicated with { InstanceId = @"USB\VID_8087&PID_0033\other" };
        checks["physical_identity_disambiguates_identical_usb_models"] = Drivers.MatchTransport("usb-id:" + dedicated.AdapterId, new[] { other, dedicated }).Single().InstanceId == dedicated.InstanceId;
        checks["physical_identity_survives_driver_binding_and_case"] = dedicated.AdapterId == (system with { InstanceId = system.InstanceId.ToLowerInvariant() }).AdapterId;
        checks["composite_function_uses_physical_parent_identity"] = (dedicated with { InstanceId = @"USB\VID_8087&PID_0033&MI_00\function", PhysicalInstanceId = dedicated.InstanceId }).AdapterId == dedicated.AdapterId;
        checks["missing_physical_identity_never_selects_other_device"] = Drivers.MatchTransport("usb-id:" + new string('0', 24), new[] { other, dedicated }).Count == 0;
        var oneChoice = new[] { ("usb-id:" + dedicated.AdapterId, 0x8087, 0x0033) };
        var twoChoices = oneChoice.Concat(new[] { ("usb-id:" + other.AdapterId, 0x8087, 0x0033) }).ToArray();
        checks["legacy_unique_choice_migrates_to_physical_identity"] = Drivers.SelectTransport("usb:8087:0033#0", oneChoice) == oneChoice[0].Item1;
        checks["legacy_identical_models_require_explicit_selection"] = Drivers.SelectTransport("usb:8087:0033#1", twoChoices) == null;
        checks["missing_saved_identity_never_falls_back_to_first_model"] = Drivers.SelectTransport("usb-id:" + other.AdapterId, oneChoice) == null;
        checks["explicit_identity_survives_inventory_reordering"] = Drivers.SelectTransport(oneChoice[0].Item1, Enumerable.Reverse(twoChoices)) == oneChoice[0].Item1;
        var driverA = new DriverCandidate("a.inf", "设备驱动", "Vendor A", 10, 2, 20, true, 0);
        var driverB = driverA with { Inf = "b.inf", Provider = "Vendor B", Rank = 50, Version = 999 };
        checks["original_driver_selection_uses_windows_rank_without_brand_preference"] = DriverSelection.Original(new[] { driverB, driverA }, "current.inf") == driverA;
        checks["original_driver_selection_rejects_bad_or_non_bluetooth_candidates"] = DriverSelection.Original(new[] { driverA with { Flags = 0x800 }, driverB with { BluetoothClass = false } }, "current.inf") == null;
        checks["original_driver_selection_does_not_reselect_current_driver"] = DriverSelection.Original(new[] { driverA }, "a.inf") == null;
        checks["driver_rank_native_struct_has_x64_layout"] = Marshal.SizeOf(typeof(Drivers).Assembly.GetType("BlueHead.Platform.DeviceNative+DriverInstallParameters")!) == 32;
        var infFolder = Path.Combine(root, "driver-fixtures"); Directory.CreateDirectory(infFolder);
        var installed = Path.Combine(infFolder, "installed.inf"); var exact = Path.Combine(infFolder, "original.inf"); var unrelated = Path.Combine(infFolder, "component.inf");
        File.WriteAllText(installed, "original driver fixture"); File.Copy(installed, exact, true); File.WriteAllText(unrelated, "another component fixture");
        checks["driver_export_selects_exact_original_inf_not_first_filename"] = DriverSelection.ExportedInf(installed, new[] { unrelated, exact }) == exact;
        try { DriverSelection.ExportedInf(installed, new[] { exact, installed }); checks["ambiguous_export_is_rejected_before_driver_binding"] = false; }
        catch (IOException) { checks["ambiguous_export_is_rejected_before_driver_binding"] = true; }
        checks["system_driver_with_backup_cannot_be_restored_again"] = !BluetoothSetup.Adapter(system).CanRestore && BluetoothSetup.Adapter(system).CanInstall;
        checks["dedicated_driver_is_not_reinstalled_to_initialize_firmware"] = !BluetoothSetup.Adapter(dedicated).CanInstall && BluetoothSetup.Adapter(dedicated).CanRestore;
        checks["missing_backup_does_not_offer_driver_restore"] = !BluetoothSetup.Adapter(dedicated with { HasBackup = false }).CanRestore;
        checks["restored_pending_restart_disables_both_driver_actions"] = !BluetoothSetup.Adapter(system with { RebootRequired = true }).CanInstall && !BluetoothSetup.Adapter(system with { RebootRequired = true }).CanRestore;
        checks["winusb_pending_restart_retains_safe_restore_path"] = BluetoothSetup.Adapter(dedicated with { RebootRequired = true }).CanRestore;
        checks["shared_mode_requires_system_driver_when_only_winusb_exists"] = BluetoothSetup.Reception(true, true, false, true, dedicated, false, true).Stage == BluetoothSetupStage.SystemRestoreRequired;
        checks["shared_mode_can_use_another_system_radio"] = BluetoothSetup.Reception(true, true, false, true, dedicated with { RebootRequired = true }, true, true).Ready;
        checks["dedicated_intent_does_not_claim_system_driver_ready"] = BluetoothSetup.Reception(false, true, false, true, system, true, false).Stage == BluetoothSetupStage.DedicatedRequired;
        checks["native_intent_after_failed_radio_initialization_requests_restart"] = BluetoothSetup.Reception(true, true, false, false, system with { RebootRequired = true, SystemRadioReady = false }, false, false).Stage == BluetoothSetupStage.RestartRequired;
        checks["no_system_radio_opens_windows_settings_instead_of_receiver"] = BluetoothSetup.Reception(true, true, false, false, null, false, false).Stage == BluetoothSetupStage.SystemBluetoothRequired;
        checks["uninspected_driver_state_is_not_pretended_ready"] = !BluetoothSetup.Reception(false, false, false, true, dedicated, false, true).Ready;
        checks["failed_readiness_check_has_explicit_retry"] = BluetoothSetup.Reception(true, false, true, false, null, false, false).Stage == BluetoothSetupStage.CheckFailed;
        checks["unhealthy_dedicated_interface_is_not_claimed_ready"] = BluetoothSetup.Reception(false, true, false, true, dedicated with { Healthy = false }, false, true).Stage == BluetoothSetupStage.Unavailable;
        checks["driver_metadata_null_service_is_safe_for_display"] = BluetoothSetup.Adapter(system with { Service = null! }).CanInstall;
        var bound = new DriverObservation(true, false, true, false, false);
        checks["driver_binding_is_not_radio_readiness"] = DriverReadiness.Evaluate(bound, false, TimeSpan.Zero, false) == DriverReadinessAction.Wait;
        checks["recovery_checks_exact_target_radio_before_success"] = DriverReadiness.Evaluate(bound with { RadioReady = true }, false, TimeSpan.Zero, false) == DriverReadinessAction.Ready;
        checks["recovery_restarts_only_once_after_initial_wait"] = DriverReadiness.Evaluate(bound, false, TimeSpan.FromSeconds(16), false) == DriverReadinessAction.RestartDevice && DriverReadiness.Evaluate(bound, false, TimeSpan.FromSeconds(16), true) == DriverReadinessAction.Wait;
        checks["bound_uninitialized_radio_requests_windows_restart"] = DriverReadiness.Evaluate(bound, false, TimeSpan.FromSeconds(45), true) == DriverReadinessAction.RestartWindows;
        checks["failed_binding_is_not_claimed_as_restored"] = DriverReadiness.Evaluate(bound with { Dedicated = true }, false, TimeSpan.FromSeconds(45), true) == DriverReadinessAction.Failed;
        checks["temporarily_disappeared_adapter_is_waited_for"] = DriverReadiness.Evaluate(new DriverObservation(), false, TimeSpan.FromSeconds(2), false) == DriverReadinessAction.Wait;
        checks["pnp_restart_takes_precedence_over_ready_interface"] = DriverReadiness.Evaluate(bound with { RestartRequired = true, RadioReady = true }, false, TimeSpan.Zero, false) == DriverReadinessAction.RestartWindows;
        checks["winusb_readiness_does_not_require_system_radio"] = DriverReadiness.Evaluate(bound with { Dedicated = true }, true, TimeSpan.Zero, false) == DriverReadinessAction.Ready;
        checks["invalid_and_default_radio_addresses_are_rejected"] = !DriverReadiness.ValidRadioAddress(0) && !DriverReadiness.ValidRadioAddress(0xffffffffffff) && !DriverReadiness.ValidRadioAddress(0x0003199e8b00) && DriverReadiness.ValidRadioAddress(0x123456789abc);
        var platform = typeof(Drivers).Assembly;
        checks["driver_recovery_native_structures_have_x64_layout"] =
            Marshal.SizeOf(platform.GetType("BlueHead.Platform.DeviceNative+PropertyChange")!) == 20 &&
            Marshal.SizeOf(platform.GetType("BlueHead.Platform.DeviceNative+InstallParameters")!) == 584 &&
            Marshal.SizeOf(platform.GetType("BlueHead.Platform.BluetoothRadio+InterfaceData")!) == 32 &&
            Marshal.SizeOf(platform.GetType("BlueHead.Platform.BluetoothRadio+RadioInfo")!) == 520;
        checks["same_legacy_driver_record_hash"] = FrameworkCompat.HashPrefix(@"USB\VID_8087&PID_0033\5&ABC&0&10") == "CF044A274D54482A6C8165B5";
        var arguments = new[] { "", "plain", "with spaces", "中英🙂", "\tline\n", @"C:\ends with slash\", "a\\\"b", "a\"b\\", @"USB\VID_8087&PID_0033\5&ABC&0&10", "&|<>^%" };
        using (var child = Start(new[] { "--echo" }.Concat(arguments).ToArray()))
        {
            var output = await child.StandardOutput.ReadToEndAsync(); await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
            checks["windows_argument_roundtrip_preserves_all_values"] = JsonSerializer.Deserialize<string[]>(output)?.SequenceEqual(arguments) == true;
        }
        try { FrameworkCompat.QuoteArgument("bad\0argument"); checks["reject_embedded_nul"] = false; }
        catch (ArgumentException) { checks["reject_embedded_nul"] = true; }
        using (var child = Start("--sleep")) { await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); checks["asynchronous_process_exit"] = child.HasExited; }
        using (var child = Start("--echo")) { child.WaitForExit(); await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); checks["already_exited_process_has_no_event_race"] = true; }
        var pending = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        try { await pending.Task.WaitAsync(TimeSpan.FromMilliseconds(30)); checks["timeout_does_not_cancel_original_task"] = false; }
        catch (TimeoutException) { pending.SetResult(17); checks["timeout_does_not_cancel_original_task"] = await pending.Task == 17; }
        var fault = Task.FromException<int>(new IOException("expected"));
        try { await fault.WaitAsync(TimeSpan.FromSeconds(1)); checks["async_exception_type_preserved"] = false; }
        catch (IOException) { checks["async_exception_type_preserved"] = true; }
        using (var child = Start("--parent-child"))
        {
            using var tree = new ProcessTree(child);
            child.StandardInput.WriteLine("start"); child.StandardInput.Flush();
            var descendantId = int.Parse(await child.StandardOutput.ReadLineAsync());
            using var descendant = Process.GetProcessById(descendantId);
            tree.Terminate(); await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); await descendant.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
            checks["owned_job_terminates_background_and_descendant"] = child.HasExited && descendant.HasExited;
        }
        using (var child = Start("--child"))
        {
            var tree = new ProcessTree(child); await child.StandardOutput.ReadLineAsync(); tree.Dispose(); tree.Dispose();
            await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); checks["job_dispose_is_idempotent_and_cleans_process"] = child.HasExited;
        }
        var path = Path.Combine(root, "atomic-record.json"); var temporary = path + ".tmp";
        File.WriteAllText(temporary, "first"); FrameworkCompat.ReplaceFile(temporary, path);
        checks["first_record_rename"] = File.ReadAllText(path) == "first" && !File.Exists(temporary);
        File.WriteAllText(temporary, "second"); FrameworkCompat.ReplaceFile(temporary, path);
        checks["existing_record_atomic_replace"] = File.ReadAllText(path) == "second" && !File.Exists(temporary);
        try { FrameworkCompat.ReplaceFile(Path.Combine(root, "missing-source"), path); checks["failed_replace_keeps_valid_record"] = false; }
        catch (IOException) { checks["failed_replace_keeps_valid_record"] = File.ReadAllText(path) == "second"; }
        checks["relative_backup_stays_in_directory"] = FrameworkCompat.RelativePath(root, Path.Combine(root, "original", "a.inf")) == Path.Combine("original", "a.inf");
        try { FrameworkCompat.RelativePath(root, Path.GetFullPath(Path.Combine(root, "..", "escape.inf"))); checks["relative_backup_rejects_directory_escape"] = false; }
        catch (IOException) { checks["relative_backup_rejects_directory_escape"] = true; }
        var utf8 = Encoding.UTF8.GetBytes("中英🙂\0"); var memory = Marshal.AllocHGlobal(utf8.Length);
        try { Marshal.Copy(utf8, 0, memory, utf8.Length); checks["native_utf8_is_not_ansi"] = FrameworkCompat.PtrToStringUtf8(memory) == "中英🙂" && FrameworkCompat.PtrToStringUtf8(IntPtr.Zero) == null; }
        finally { Marshal.FreeHGlobal(memory); }
        checks["finite_and_clamp_preserve_volume_contract"] = !FrameworkCompat.IsFinite(double.NaN) && !FrameworkCompat.IsFinite(double.PositiveInfinity) && FrameworkCompat.IsFinite(2) && FrameworkCompat.Clamp(-1, 0, 2) == 0 && FrameworkCompat.Clamp(3d, 0, 2) == 2;
    }
}
