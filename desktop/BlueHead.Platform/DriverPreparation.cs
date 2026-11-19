using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace BlueHead.Platform;

public static partial class Drivers
{
    private static bool PrepareControllerRestore(DriverRecord record)
    {
        var device = Enumerate().Single(item => item.InstanceId.Equals(record.InstanceId, StringComparison.OrdinalIgnoreCase));
        if (!device.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase)) return false;
        record.Stage = "initializing_before_restore"; Save(record);
        var executable = Path.Combine(AppContext.BaseDirectory, "engine", "BlueHead.Runtime.exe");
        try
        {
            if (!File.Exists(executable)) throw new IOException("启动固件工具缺失");
            var start = new ProcessStartInfo(executable) { UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
            start.AddArgument("--prepare-controller");
            start.AddArgument("--adapter-id"); start.AddArgument(device.AdapterId);
            start.AddArgument("--parent-pid"); start.AddArgument(Process.GetCurrentProcess().Id.ToString(System.Globalization.CultureInfo.InvariantCulture));
            using var process = Process.Start(start) ?? throw new IOException("无法启动固件初始化工具");
            ProcessTree owned;
            try { owned = new ProcessTree(process); }
            catch
            {
                if (!process.HasExited) process.Kill();
                process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
                throw;
            }
            using var tree = owned;
            var stdout = process.StandardOutput.ReadToEndAsync(); var stderr = process.StandardError.ReadToEndAsync();
            try { process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(75)).GetAwaiter().GetResult(); }
            catch (TimeoutException)
            {
                tree.Terminate(); process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
                throw new IOException("蓝牙启动固件初始化超时");
            }
            Task.WhenAll(stdout, stderr).GetAwaiter().GetResult();
            if (process.ExitCode != 0) throw new IOException("蓝牙启动固件初始化未完成：" + stderr.Result.Trim());
            using var result = JsonDocument.Parse(stdout.Result);
            var state = result.RootElement.GetProperty("firmware_state").GetString();
            var hci = result.RootElement.GetProperty("capabilities").GetProperty("hci");
            if (!hci.GetProperty("ready").GetBoolean()) throw new IOException("蓝牙控制器就绪状态未确认");
            if (hci.TryGetProperty("address", out var original))
            {
                EnsureAddressProfile(record, original.GetString(), device.PhysicalInstanceId);
                if (!string.Equals(original.GetString(), record.OriginalAddress, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("原蓝牙地址尚未还原，请重新检测后修复。");
            }
            record.FirmwarePreparation = state;
        }
        catch (Exception error) when (error is IOException or Win32Exception or JsonException or InvalidOperationException or KeyNotFoundException)
        {
            // Firmware preparation is an aid, never a prerequisite for getting
            // the original driver back. It can also initialize the controller.
            record.FirmwarePreparation = "incomplete: " + error.Message;
        }
        Save(record);
        return record.FirmwarePreparation?.StartsWith("incomplete:", StringComparison.Ordinal) == false;
    }

    /// <summary>One bounded handoff. Each driver owner is released before the next is opened.</summary>
    public static Task<DriverRecord> InitializeOriginalAsync(string instanceId) => Task.Run(() =>
    {
        RequireAdministrator(); RequireReceiverStopped(instanceId);
        using (var lease = new DriverOperationLease())
        {
            RequireReceiverStopped(instanceId);
            var record = ReadRecord(instanceId) ?? BackupAsync(instanceId).GetAwaiter().GetResult();
            try
            {
                record.Error = null;
                // Reopen/initialize the dedicated controller first. Escalate to
                // an exact device restart, then one OEM handoff only if needed.
                var current = Enumerate().Single(item => item.InstanceId.Equals(instanceId, StringComparison.OrdinalIgnoreCase));
                if (current.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase))
                {
                    using var audio = QuiesceDriverAudio(record);
                    if (!current.RebootRequired && PrepareControllerRestore(record))
                    { record.Stage = "winusb_ready"; record.RebootRequired = false; Save(record); return record; }
                    record.Stage = "restarting_device"; Save(record);
                    try
                    {
                        RestartExactDevice(instanceId, record); WaitForDriverReady(record, true);
                        if (!record.RebootRequired && PrepareControllerRestore(record))
                        { record.Stage = "winusb_ready"; record.Error = null; Save(record); return record; }
                    }
                    catch (Exception retry) when (retry is IOException or Win32Exception)
                    { record.Error = retry.Message; Save(record); }
                }
                var result = DriverHandoff.Run(new WindowsDriverHandoff(record));
                if (result == DriverHandoffOutcome.DedicatedReady)
                {
                    record.Stage = "winusb_ready"; record.Error = null; Save(record); return record;
                }
                // A rebind/reset can erase RAM. Never loop between drivers and
                // never leave an unverified dedicated controller as 'ready'.
                record.Error = record.RebootRequired ? "驱动恢复完成，请重启电脑。" : "已恢复系统蓝牙。此适配器未能在驱动交接后保留可用固件；可使用 Windows 共用接收，或换用其他适配器。";
                Save(record); return record;
            }
            catch (Exception error)
            {
                record.Error = error.Message; record.Stage = "bootstrap_failed"; Save(record);
                try { RestoreCore(record, false); record.Error = "原厂引导未完成，已恢复系统蓝牙。" + error.Message; Save(record); }
                catch (Exception rollback) { record.Stage = "recovery_required"; record.Error = error.Message + "；请恢复系统蓝牙：" + rollback.Message; Save(record); throw; }
                return record;
            }
        }
    });

    private sealed class WindowsDriverHandoff(DriverRecord record) : IDriverHandoff
    {
        public bool RestartRequired => record.RebootRequired;
        public bool SystemReady => record.SystemRadioReady;
        public void Stage(string name) { record.Stage = name; Save(record); }
        public void RestoreOriginal() => RestoreCore(record, false);
        public bool ProbeDedicated() => PrepareControllerRestore(record);
        public void BindDedicated()
        {
            var package = Path.Combine(RecordDirectory(record.InstanceId), "winusb"); SecureDirectory(package);
            LibWdi.Prepare(record.InstanceId, package);
            Stage("bootstrap_binding_winusb");
            BindAndConfirm(record, Path.Combine(package, "bluehead.inf"), true);
        }
    }

    private static bool RestartExactDevice(string instanceId, DriverRecord? record = null)
    {
        var (set, device) = Open(instanceId);
        try
        {
            // This also handles DN_NEED_RESTART cases where pnputil's ordinary
            // restart refuses to proceed. The exact HCI device is validated by Open.
            // cfgmgr32 specifies MAX_PATH. A zeroed bounded buffer also keeps
            // native veto diagnostics from exposing an uninitialized tail.
            var vetoBuffer = new char[260];
            // CM_REMOVE_UI_NOT_OK only. NO_RESTART would require RESET before
            // READY; using NO_RESTART + READY previously left devices stopped.
            var removed = DeviceNative.CM_Query_And_Remove_SubTreeW(device.Instance, out var veto, vetoBuffer, (uint)vetoBuffer.Length, 1);
            int end = Array.IndexOf(vetoBuffer, '\0');
            var name = new string(vetoBuffer, 0, end < 0 ? vetoBuffer.Length : end);
            if (removed == 0)
            {
                var ready = DeviceNative.CM_Setup_DevNode(device.Instance, 0);
                if (ready != 0) throw new IOException($"蓝牙设备已释放，重新启用尚未完成（{ready}），请重新检测。");
                return false;
            }
            if (record != null && veto != 0)
            {
                record.ReleaseDiagnostic = "Windows 仍占用此蓝牙的" +
                    (name.StartsWith("BTHENUM", StringComparison.OrdinalIgnoreCase) ? "通话或声音连接" : "设备连接") +
                    $"（CM {removed}，占用类型 {veto}，{name}）。";
                Save(record);
            }
            var parameters = new DeviceNative.PropertyChange { HeaderSize = 8, Function = 0x12, State = 3, Scope = 1 };
            if (!DeviceNative.SetupDiSetClassInstallParams(set, ref device, ref parameters, Marshal.SizeOf<DeviceNative.PropertyChange>()) ||
                !DeviceNative.SetupDiCallClassInstaller(0x12, set, ref device))
                throw new Win32Exception(Marshal.GetLastWin32Error(),
                    $"Windows 仍占用此蓝牙的" + (name.StartsWith("BTHENUM", StringComparison.OrdinalIgnoreCase) ? "通话或声音连接" : "设备连接") +
                    $"，暂未完成驱动切换（CM {removed}，占用类型 {veto}，{name}）。请先断开发送设备的 Windows 蓝牙连接，再点击重新检测。");
            var installed = new DeviceNative.InstallParameters { Size = (uint)Marshal.SizeOf<DeviceNative.InstallParameters>(), DriverPath = "" };
            return DeviceNative.SetupDiGetDeviceInstallParams(set, ref device, ref installed) && (installed.Flags & (0x80 | 0x100)) != 0;
        }
        finally { DeviceNative.SetupDiDestroyDeviceInfoList(set); }
    }

    private static void WaitForDriverReady(DriverRecord record, bool dedicated)
    {
        var elapsed = Stopwatch.StartNew(); bool restarted = false;
        var observations = new DriverReadinessTracker();
        while (true)
        {
            var current = Enumerate().SingleOrDefault(item => item.InstanceId.Equals(record.InstanceId, StringComparison.OrdinalIgnoreCase));
            var state = current == null ? new DriverObservation() : new DriverObservation(true,
                current.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase), current.Healthy,
                current.RebootRequired, !dedicated && BluetoothRadio.IsReady(record.InstanceId) &&
                (record.OriginalAddress.Length == 0 || BluetoothRadio.Address(record.InstanceId) == record.OriginalAddress));
            switch (observations.Observe(state, dedicated, elapsed.Elapsed, restarted))
            {
                case DriverReadinessAction.Ready:
                    record.RebootRequired = false; record.ReleaseDiagnostic = null; record.SystemRadioReady = !dedicated; return;
                case DriverReadinessAction.RestartWindows:
                    record.RebootRequired = true; record.SystemRadioReady = false; return;
                case DriverReadinessAction.Failed:
                    throw new IOException(current == null ? "蓝牙设备暂时未出现，请检查连接后点击“重新检测”。" : "蓝牙设备尚未准备好，请点击“重新检测”；持续失败时再执行修复。当前没有确认必须重启电脑。");
                case DriverReadinessAction.RestartDevice:
                    restarted = true;
                    try { RestartExactDevice(record.InstanceId, record); }
                    catch (Win32Exception error) { record.ReleaseDiagnostic = error.Message; Save(record); }
                    break;
            }
            Thread.Sleep(250);
        }
    }
}
