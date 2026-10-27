using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace BlueHead.Platform;

public static partial class Drivers
{
    private static void PrepareControllerRestore(DriverRecord record)
    {
        var device = Enumerate().Single(item => item.InstanceId.Equals(record.InstanceId, StringComparison.OrdinalIgnoreCase));
        if (!device.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase)) return;
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
            if (state is not ("loaded" or "already_loaded")) throw new IOException("蓝牙启动固件状态未确认");
            record.FirmwarePreparation = state;
        }
        catch (Exception error) when (error is IOException or Win32Exception or JsonException or InvalidOperationException or KeyNotFoundException)
        {
            // Firmware preparation is an aid, never a prerequisite for getting
            // the original driver back. It can also initialize the controller.
            record.FirmwarePreparation = "incomplete: " + error.Message;
        }
        Save(record);
    }

    private static bool RestartExactDevice(string instanceId)
    {
        var (set, device) = Open(instanceId);
        try
        {
            var parameters = new DeviceNative.PropertyChange { HeaderSize = 8, Function = 0x12, State = 3, Scope = 1 };
            if (!DeviceNative.SetupDiSetClassInstallParams(set, ref device, ref parameters, Marshal.SizeOf<DeviceNative.PropertyChange>()) ||
                !DeviceNative.SetupDiCallClassInstaller(0x12, set, ref device))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "无法重新初始化这个蓝牙适配器");
            var installed = new DeviceNative.InstallParameters { Size = (uint)Marshal.SizeOf<DeviceNative.InstallParameters>(), DriverPath = "" };
            return DeviceNative.SetupDiGetDeviceInstallParams(set, ref device, ref installed) && (installed.Flags & (0x80 | 0x100)) != 0;
        }
        finally { DeviceNative.SetupDiDestroyDeviceInfoList(set); }
    }

    private static void WaitForDriverReady(DriverRecord record, bool dedicated)
    {
        var elapsed = Stopwatch.StartNew(); bool restarted = false;
        while (true)
        {
            var current = Enumerate().SingleOrDefault(item => item.InstanceId.Equals(record.InstanceId, StringComparison.OrdinalIgnoreCase));
            var state = current == null ? new DriverObservation() : new DriverObservation(true,
                current.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase), current.Healthy,
                current.RebootRequired, !dedicated && BluetoothRadio.IsReady(record.InstanceId));
            switch (DriverReadiness.Evaluate(state, dedicated, elapsed.Elapsed, restarted))
            {
                case DriverReadinessAction.Ready:
                    record.RebootRequired = false; record.SystemRadioReady = !dedicated; return;
                case DriverReadinessAction.RestartWindows:
                    record.RebootRequired = true; record.SystemRadioReady = false; return;
                case DriverReadinessAction.Failed:
                    throw new IOException("目标驱动尚未绑定到这个蓝牙适配器，请保持连接后重试。");
                case DriverReadinessAction.RestartDevice:
                    restarted = true;
                    try { if (RestartExactDevice(record.InstanceId)) { record.RebootRequired = true; return; } }
                    catch (Win32Exception) { /* A pending PnP operation may need Windows restart; keep observing. */ }
                    break;
            }
            Thread.Sleep(250);
        }
    }
}
