using System.Text.Json;
using System.Diagnostics;
using BlueHead.Platform;

namespace BlueHead.DriverHelper;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        var quiet = args.Contains("--quiet");
        args = args.Where(arg => arg != "--quiet").ToArray();
        try
        {
            if (args.Length == 1 && args[0] == "--desktop-running") return Drivers.DesktopRunning() ? 10 : 0;
            if (args.Length == 1 && args[0] == "--needs-restore") return ManagedAdapters().Count > 0 ? 10 : 0;
            if (args.Length == 1 && args[0] == "--restore-all-elevate")
            {
                if (ManagedAdapters().Count == 0) return 0;
                var start = new ProcessStartInfo(FrameworkCompat.ProcessPath) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
                start.AddArgument("--restore-all");
                using var child = Process.Start(start) ?? throw new IOException("无法启动驱动恢复。");
                child.WaitForExit(); return child.ExitCode;
            }
            if (args.Length == 1 && args[0] == "--restore-all")
            {
                var restart = false;
                foreach (var device in ManagedAdapters()) restart |= Drivers.RestoreAsync(device.InstanceId).GetAwaiter().GetResult().RebootRequired;
                if (restart) return 3010;
                return ManagedAdapters().Count == 0 ? 0 : 1;
            }
            if (args.Length == 1 && args[0] == "--list") { Console.WriteLine(JsonSerializer.Serialize(Drivers.Enumerate())); return 0; }
            if (args.Length == 2 && args[0] == "--compatible") { Console.WriteLine(JsonSerializer.Serialize(Drivers.CompatibleDrivers(args[1]).Select(item => new { item.Inf, item.Description, item.Provider }))); return 0; }
            if (args.Length == 1 && args[0] == "--self-test")
            {
                var devices = Drivers.Enumerate();
                Console.WriteLine(JsonSerializer.Serialize(new { passed = true, device_count = devices.Count, readonly_inventory = true, device_struct_size = System.Runtime.InteropServices.Marshal.SizeOf<DeviceNativeProbe>() })); return 0;
            }
            if (args.Length == 2 && args[0] is "--install" or "--restore" or "--backup" or "--initialize-original" or "--repair-system")
            {
                var operation = args[0]; var instance = args[1];
                // Wait without pumping a second arbitrary IPC command: this process performs one exact operation.
                var task = operation == "--install" ? Drivers.InstallAsync(instance) : operation is "--restore" or "--repair-system" ? Drivers.RestoreAsync(instance) : operation == "--initialize-original" ? Drivers.InitializeOriginalAsync(instance) : Drivers.BackupAsync(instance);
                var record = task.GetAwaiter().GetResult(); Console.WriteLine(JsonSerializer.Serialize(record));
                // The desktop presents the final state and exact recovery cause.
                // Headless validation must never wait behind a modal message box.
                return record.RebootRequired ? 3010 : 0;
            }
            Console.Error.WriteLine("Usage: --list | --compatible INSTANCE | --backup INSTANCE | --install INSTANCE | --restore INSTANCE | --repair-system INSTANCE | --initialize-original INSTANCE | --self-test"); return 2;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            if (!quiet && args.Any(arg => arg is "--install" or "--restore" or "--repair-system" or "--initialize-original" or "--restore-all" or "--restore-all-elevate")) System.Windows.Forms.MessageBox.Show(error.Message, "BlueHead · 驱动操作未完成", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
    }
    private static List<DriverDevice> ManagedAdapters() => Drivers.Enumerate().Where(device => device.HasBackup && device.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase)).ToList();
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct DeviceNativeProbe { public uint Size; public Guid ClassGuid; public uint Instance; public IntPtr Reserved; }
}
