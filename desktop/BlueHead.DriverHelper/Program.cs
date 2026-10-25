using System.Text.Json;
using System.Diagnostics;
using BlueHead.Platform;

namespace BlueHead.DriverHelper;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args is ["--desktop-running"]) return Drivers.DesktopRunning() ? 10 : 0;
            if (args is ["--needs-restore"]) return ManagedAdapters().Count > 0 ? 10 : 0;
            if (args is ["--restore-all-elevate"])
            {
                if (ManagedAdapters().Count == 0) return 0;
                var start = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
                start.ArgumentList.Add("--restore-all");
                using var child = Process.Start(start) ?? throw new IOException("无法启动驱动恢复。");
                child.WaitForExit(); return child.ExitCode;
            }
            if (args is ["--restore-all"])
            {
                foreach (var device in ManagedAdapters()) Drivers.RestoreAsync(device.InstanceId).GetAwaiter().GetResult();
                return ManagedAdapters().Count == 0 ? 0 : 1;
            }
            if (args is ["--list"]) { Console.WriteLine(JsonSerializer.Serialize(Drivers.Enumerate())); return 0; }
            if (args is ["--compatible", var id]) { Console.WriteLine(JsonSerializer.Serialize(Drivers.CompatibleDrivers(id).Select(item => new { item.Inf, item.Description, item.Provider }))); return 0; }
            if (args is ["--self-test"])
            {
                var devices = Drivers.Enumerate();
                Console.WriteLine(JsonSerializer.Serialize(new { passed = true, device_count = devices.Count, readonly_inventory = true, device_struct_size = System.Runtime.InteropServices.Marshal.SizeOf<DeviceNativeProbe>() })); return 0;
            }
            if (args is [var operation, var instance] && operation is "--install" or "--restore" or "--backup")
            {
                // Wait without pumping a second arbitrary IPC command: this process performs one exact operation.
                var task = operation == "--install" ? Drivers.InstallAsync(instance) : operation == "--restore" ? Drivers.RestoreAsync(instance) : Drivers.BackupAsync(instance);
                var record = task.GetAwaiter().GetResult(); Console.WriteLine(JsonSerializer.Serialize(record)); return 0;
            }
            Console.Error.WriteLine("Usage: --list | --compatible INSTANCE | --backup INSTANCE | --install INSTANCE | --restore INSTANCE | --self-test"); return 2;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            if (args.Any(arg => arg is "--install" or "--restore" or "--restore-all" or "--restore-all-elevate")) System.Windows.Forms.MessageBox.Show(error.Message, "BlueHead · 驱动操作未完成", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
    }
    private static List<DriverDevice> ManagedAdapters() => Drivers.Enumerate().Where(device => device.HasBackup && device.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase)).ToList();
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct DeviceNativeProbe { public uint Size; public Guid ClassGuid; public uint Instance; public IntPtr Reserved; }
}
