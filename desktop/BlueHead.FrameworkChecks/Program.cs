using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using BlueHead.Platform;

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
        var restartAdapter = new DriverDevice(@"USB\VID_8087&PID_0033\offline", "offline", "WinUSB", "offline.inf", "", false, false) { RebootRequired = true };
        checks["restart_matches_selected_adapter"] = Drivers.RequiresRestart("usb:8087:0033#0", [restartAdapter]) && !Drivers.RequiresRestart("usb:1111:2222#0", [restartAdapter]);
        checks["duplicate_usb_models_do_not_guess_instance_order"] = !Drivers.RequiresRestart("usb:8087:0033#0", [restartAdapter, restartAdapter with { InstanceId = @"USB\VID_8087&PID_0033\another", RebootRequired = false }]);
        checks["driver_restart_state_roundtrips"] = JsonSerializer.Deserialize<DriverDevice>(JsonSerializer.Serialize(restartAdapter))?.RebootRequired == true && restartAdapter.DisplayName.Contains("需要重启 Windows");
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
