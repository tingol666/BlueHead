using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace BlueHead.Platform;

public record DriverDevice(string InstanceId, string Name, string Service, string Inf, string HardwareIds, bool Healthy, bool HasBackup)
{
    public bool RebootRequired { get; init; }
    public override string ToString() => DisplayName;
    public string DisplayName => $"{Name} · {(RebootRequired ? "需要重启 Windows" : Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase) ? "专用接收" : "系统蓝牙")}";
}
public sealed class DriverRecord
{
    public int Schema { get; set; } = 1;
    public string InstanceId { get; set; } = "";
    public string OriginalInf { get; set; } = "";
    public string OriginalDescription { get; set; } = "";
    public string OriginalProvider { get; set; } = "";
    public string BackupInf { get; set; } = "";
    public string Stage { get; set; } = "backed_up";
    public string? Error { get; set; }
    public DateTime UpdatedUtc { get; set; } = DateTime.UtcNow;
    public bool RebootRequired { get; set; }
    public bool SystemRadioReady { get; set; }
    public string? FirmwarePreparation { get; set; }
}
public static partial class Drivers
{
    public static bool RequiresRestart(string transport, IEnumerable<DriverDevice> devices)
    {
        var match = Regex.Match(transport, @"^usb:([0-9a-f]{4}):([0-9a-f]{4})#\d+(?:\+sco=0)?$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (!match.Success) return false;
        var prefix = $@"USB\VID_{match.Groups[1].Value}&PID_{match.Groups[2].Value}";
        var matching = devices.Where(device => device.InstanceId.StartsWith(prefix + @"\", StringComparison.OrdinalIgnoreCase)
            || device.InstanceId.StartsWith(prefix + "&MI_", StringComparison.OrdinalIgnoreCase)).ToArray();
        // USB enumeration order can differ between SetupAPI and libusb. Only
        // gate an unambiguous physical adapter; list per-device state otherwise.
        return matching.Length == 1 && matching[0].RebootRequired;
    }
    public static string Store => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "BlueHead", "Drivers");
    private static readonly Regex instancePattern = new(@"^USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:&MI_[0-9A-F]{2})?\\[^\r\n]{1,200}$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
    private static Regex InstancePattern() => instancePattern;
    public static string RecordDirectory(string instanceId) => Path.Combine(Store, FrameworkCompat.HashPrefix(instanceId.ToUpperInvariant()));
    public static DriverRecord? ReadRecord(string instanceId)
    {
        try
        {
            var folder = RecordDirectory(instanceId);
            var info = new DirectoryInfo(folder);
            if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0) return null;
            var owner = info.GetAccessControl().GetOwner(typeof(SecurityIdentifier));
            if (owner?.Value != "S-1-5-32-544" && owner?.Value != "S-1-5-18") return null;
            var record = JsonSerializer.Deserialize<DriverRecord>(File.ReadAllText(Path.Combine(folder, "recovery.json")));
            if (record?.InstanceId.Equals(instanceId, StringComparison.OrdinalIgnoreCase) != true) return null;
            var backup = Path.GetFullPath(Path.Combine(folder, record.BackupInf));
            return backup.StartsWith(folder + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ? record : null;
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException or ArgumentException) { return null; }
    }
    public static List<DriverDevice> Enumerate()
    {
        var result = new List<DriverDevice>();
        var set = DeviceNative.SetupDiGetClassDevs(IntPtr.Zero, "USB", IntPtr.Zero, DeviceNative.Present | DeviceNative.AllClasses);
        if (set == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        try
        {
            for (uint i = 0; ; i++)
            {
                var device = DeviceNative.NewDevice();
                if (!DeviceNative.SetupDiEnumDeviceInfo(set, i, ref device)) break;
                var instance = DeviceNative.InstanceId(set, ref device);
                var service = DeviceNative.Property(set, ref device, 4);
                var compatible = DeviceNative.Property(set, ref device, 2);
                var hardware = DeviceNative.Property(set, ref device, 1);
                // USB Bluetooth HCI interfaces only. Never infer eligibility from a friendly name.
                if (!InstancePattern().IsMatch(instance) || !compatible.Contains("Class_E0&SubClass_01&Prot_01", StringComparison.OrdinalIgnoreCase)) continue;
                var name = DeviceNative.Property(set, ref device, 12);
                if (name.Length == 0) name = DeviceNative.Property(set, ref device, 0);
                var queried = DeviceNative.CM_Get_DevNode_Status(out var status, out var problem, device.Instance, 0) == 0;
                var restart = queried && (status & 0x100) != 0; // DN_NEED_RESTART
                var record = ReadRecord(instance);
                // A firmware timeout can require a restart even without
                // DN_NEED_RESTART. Re-check live radio state after reboot.
                if (!restart && !service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase) &&
                    record?.Stage == "restored_restart_required" && record.RebootRequired)
                    restart = !BluetoothRadio.IsReady(instance);
                result.Add(new(instance, name, service, DeviceNative.DriverInf(set, ref device), hardware.Replace('\0', ';'), queried && problem == 0 && (status & 8) != 0 && !restart, record != null) { RebootRequired = restart });
            }
            return result;
        }
        finally { DeviceNative.SetupDiDestroyDeviceInfoList(set); }
    }
    private static void SecureDirectory(string path)
    {
        var parent = Path.GetDirectoryName(path);
        if (parent != null && Directory.Exists(parent) && (File.GetAttributes(parent) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("恢复目录不能位于目录链接中。");
        if (Directory.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("恢复目录不能是目录链接。");
        Directory.CreateDirectory(path);
        var rules = new DirectorySecurity();
        rules.SetAccessRuleProtection(true, false);
        var inheritance = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
        foreach (var sid in new[] { WellKnownSidType.BuiltinAdministratorsSid, WellKnownSidType.LocalSystemSid })
            rules.AddAccessRule(new(new SecurityIdentifier(sid, null), FileSystemRights.FullControl, inheritance, PropagationFlags.None, AccessControlType.Allow));
        rules.AddAccessRule(new(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, null), FileSystemRights.ReadAndExecute, inheritance, PropagationFlags.None, AccessControlType.Allow));
        rules.SetOwner(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null));
        new DirectoryInfo(path).SetAccessControl(rules);
    }
    private static void Save(DriverRecord record)
    {
        record.UpdatedUtc = DateTime.UtcNow;
        var path = Path.Combine(RecordDirectory(record.InstanceId), "recovery.json");
        File.WriteAllText(path + ".tmp", JsonSerializer.Serialize(record, new JsonSerializerOptions { WriteIndented = true }));
        FrameworkCompat.ReplaceFile(path + ".tmp", path);
    }
    private static (IntPtr Set, DeviceNative.DeviceInfo Device) Open(string instanceId)
    {
        if (!InstancePattern().IsMatch(instanceId)) throw new ArgumentException("无效 USB 设备实例。");
        var eligible = Enumerate().SingleOrDefault(item => item.InstanceId.Equals(instanceId, StringComparison.OrdinalIgnoreCase))
            ?? throw new InvalidOperationException("所选设备已断开或不是蓝牙 HCI 适配器。");
        var set = DeviceNative.SetupDiGetClassDevs(IntPtr.Zero, "USB", IntPtr.Zero, DeviceNative.Present | DeviceNative.AllClasses);
        for (uint i = 0; ; i++)
        {
            var device = DeviceNative.NewDevice();
            if (!DeviceNative.SetupDiEnumDeviceInfo(set, i, ref device)) break;
            if (DeviceNative.InstanceId(set, ref device).Equals(eligible.InstanceId, StringComparison.OrdinalIgnoreCase)) return (set, device);
        }
        DeviceNative.SetupDiDestroyDeviceInfoList(set);
        throw new IOException("所选适配器已断开。");
    }
    public static List<(string Inf, string Description, string Provider)> CompatibleDrivers(string instanceId)
    {
        var (set, device) = Open(instanceId);
        var result = new List<(string, string, string)>();
        try
        {
            if (!DeviceNative.SetupDiBuildDriverInfoList(set, ref device, DeviceNative.CompatibleDriver)) throw new Win32Exception(Marshal.GetLastWin32Error());
            for (uint i = 0; ; i++)
            {
                var driver = DeviceNative.NewDriver();
                if (!DeviceNative.SetupDiEnumDriverInfo(set, ref device, DeviceNative.CompatibleDriver, i, ref driver)) break;
                result.Add((DeviceNative.DetailInf(set, ref device, ref driver), driver.Description, driver.Provider));
            }
            return result;
        }
        finally { DeviceNative.SetupDiDestroyDriverInfoList(set, ref device, DeviceNative.CompatibleDriver); DeviceNative.SetupDiDestroyDeviceInfoList(set); }
    }
    private static async Task RunPnPAsync(params string[] arguments)
    {
        var start = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "pnputil.exe")) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var argument in arguments) start.AddArgument(argument);
        using var process = Process.Start(start) ?? throw new IOException("无法启动驱动管理工具。");
        var stdout = process.StandardOutput.ReadToEndAsync(); var stderr = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(90));
        await Task.WhenAll(stdout, stderr);
        if (process.ExitCode != 0 && process.ExitCode != 3010) throw new IOException($"驱动备份失败，Windows 返回 {process.ExitCode}。");
    }
    public static async Task<DriverRecord> BackupAsync(string instanceId)
    {
        RequireAdministrator();
        var device = Enumerate().Single(item => item.InstanceId.Equals(instanceId, StringComparison.OrdinalIgnoreCase));
        SecureDirectory(Path.GetDirectoryName(Store)!); SecureDirectory(Store);
        var folder = RecordDirectory(instanceId); SecureDirectory(folder);
        var existing = ReadRecord(instanceId);
        if (existing != null && File.Exists(Path.Combine(folder, existing.BackupInf))) return existing;
        var original = device.Inf;
        string description = device.Name, provider = "";
        if (device.Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase))
        {
            // Adopt an adapter previously prepared with Zadig. Find its actual installed OEM Bluetooth driver.
            var candidate = CompatibleDrivers(instanceId)
                .Where(item => !item.Inf.Equals(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "INF", device.Inf), StringComparison.OrdinalIgnoreCase))
                .Where(item => item.Description.Contains("Bluetooth", StringComparison.OrdinalIgnoreCase))
                .OrderByDescending(item => item.Provider.Contains("Intel", StringComparison.OrdinalIgnoreCase)).FirstOrDefault();
            if (candidate.Inf == null) throw new IOException("未找到可备份的原蓝牙驱动。请先恢复原厂驱动，再启用专用接收。");
            original = Path.GetFileName(candidate.Inf); description = candidate.Description; provider = candidate.Provider;
        }
        var backup = Path.Combine(folder, "original"); SecureDirectory(backup);
        if (original.StartsWith("oem", StringComparison.OrdinalIgnoreCase)) await RunPnPAsync("/export-driver", original, backup);
        else
        {
            // Inbox drivers are part of Windows and stay in the driver store; preserve a reference, not system binaries.
            var reference = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "INF", original);
            if (!File.Exists(reference)) throw new IOException("无法定位 Windows 原始驱动。");
            File.WriteAllText(Path.Combine(backup, "inbox-reference.txt"), original);
        }
        var inf = Directory.GetFiles(backup, "*.inf", SearchOption.AllDirectories).FirstOrDefault();
        if (original.StartsWith("oem", StringComparison.OrdinalIgnoreCase) && inf == null) throw new IOException("原驱动备份不完整，未更换驱动。");
        var record = new DriverRecord { InstanceId = instanceId, OriginalInf = original, OriginalDescription = description, OriginalProvider = provider,
            BackupInf = inf == null ? "original/inbox-reference.txt" : FrameworkCompat.RelativePath(folder, inf) };
        Save(record); return record;
    }
    private static bool Bind(string instanceId, string infPath)
    {
        var destination = new StringBuilder(1024);
        if (!DeviceNative.SetupCopyOEMInf(infPath, Path.GetDirectoryName(infPath), 1, 0, destination, destination.Capacity, out _, IntPtr.Zero))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "无法将驱动包加入 Windows 驱动仓库。");
        var wanted = Path.GetFileName(destination.ToString());
        if (wanted.Length == 0) wanted = Path.GetFileName(infPath);
        var (set, device) = Open(instanceId);
        try
        {
            if (!DeviceNative.SetupDiBuildDriverInfoList(set, ref device, DeviceNative.CompatibleDriver)) throw new Win32Exception(Marshal.GetLastWin32Error());
            for (uint i = 0; ; i++)
            {
                var driver = DeviceNative.NewDriver();
                if (!DeviceNative.SetupDiEnumDriverInfo(set, ref device, DeviceNative.CompatibleDriver, i, ref driver)) break;
                if (!Path.GetFileName(DeviceNative.DetailInf(set, ref device, ref driver)).Equals(wanted, StringComparison.OrdinalIgnoreCase)) continue;
                if (!DeviceNative.DiInstallDevice(IntPtr.Zero, set, ref device, ref driver, 0, out var reboot))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Windows 未能绑定所选驱动。");
                return reboot;
            }
            throw new IOException("目标驱动不在此适配器的兼容驱动列表中，已停止操作。");
        }
        finally { DeviceNative.SetupDiDestroyDriverInfoList(set, ref device, DeviceNative.CompatibleDriver); DeviceNative.SetupDiDestroyDeviceInfoList(set); }
    }
    public static Task<DriverRecord> InstallAsync(string instanceId) => Task.Run(() =>
    {
        RequireAdministrator();
        RequireReceiverStopped(instanceId);
        using var lease = new Mutex(false, @"Global\BlueHead.DriverOperation");
        if (!lease.WaitOne(0)) throw new IOException("另一个驱动操作正在执行。");
        try
        {
            RequireReceiverStopped(instanceId);
            var record = BackupAsync(instanceId).GetAwaiter().GetResult();
            if (Enumerate().Single(item => item.InstanceId.Equals(instanceId, StringComparison.OrdinalIgnoreCase)).Service.Equals("WinUSB", StringComparison.OrdinalIgnoreCase)) return record;
            record.Stage = "preparing_winusb"; record.Error = null; Save(record);
            try
            {
                var package = Path.Combine(RecordDirectory(instanceId), "winusb"); SecureDirectory(package);
                LibWdi.Prepare(instanceId, package);
                record.Stage = "binding_winusb"; Save(record);
                record.RebootRequired = Bind(instanceId, Path.Combine(package, "bluehead.inf"));
                record.SystemRadioReady = false;
                if (!record.RebootRequired) WaitForDriverReady(record, true);
                record.Stage = record.RebootRequired ? "winusb_restart_required" : "winusb_ready"; Save(record); return record;
            }
            catch (Exception error)
            {
                record.Stage = "failed"; record.Error = error.Message; Save(record);
                try { RestoreCore(record); } catch (Exception rollback) { record.Stage = "recovery_required"; record.Error += "；恢复失败：" + rollback.Message; Save(record); }
                throw;
            }
        }
        finally { lease.ReleaseMutex(); }
    });
    private static DriverRecord RestoreCore(DriverRecord record)
    {
        PrepareIntelRestore(record);
        record.Stage = "restoring"; record.Error = null; Save(record);
        var inf = Path.Combine(RecordDirectory(record.InstanceId), record.BackupInf);
        if (Path.GetExtension(inf) != ".inf") inf = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "INF", record.OriginalInf);
        record.RebootRequired = Bind(record.InstanceId, inf);
        record.SystemRadioReady = false;
        if (!record.RebootRequired) WaitForDriverReady(record, false);
        record.Error = record.RebootRequired ? "驱动恢复完成，请重启电脑。" : null;
        record.Stage = record.RebootRequired ? "restored_restart_required" : "restored"; Save(record); return record;
    }
    public static Task<DriverRecord> RestoreAsync(string instanceId) => Task.Run(() =>
    {
        RequireAdministrator();
        RequireReceiverStopped(instanceId);
        using var lease = new Mutex(false, @"Global\BlueHead.DriverOperation");
        if (!lease.WaitOne(0)) throw new IOException("另一个驱动操作正在执行。");
        try
        {
            RequireReceiverStopped(instanceId);
            var record = ReadRecord(instanceId) ?? BackupAsync(instanceId).GetAwaiter().GetResult();
            try { return RestoreCore(record); }
            catch (Exception error) { record.Stage = "recovery_required"; record.Error = error.Message; Save(record); throw; }
        }
        finally { lease.ReleaseMutex(); }
    });
    private static void RequireAdministrator()
    {
        if (!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator)) throw new UnauthorizedAccessException("更换或恢复驱动需要 Windows 管理员授权。");
    }
    public static bool DesktopRunning()
    {
        try { if (Mutex.TryOpenExisting("Local\\BlueHead.Desktop." + Environment.UserName, out var mutex)) { mutex?.Dispose(); return true; } }
        catch (UnauthorizedAccessException) { return true; }
        return false;
    }
    private static void RequireReceiverStopped(string instanceId)
    {
        var match = Regex.Match(instanceId, @"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", RegexOptions.IgnoreCase);
        if (!match.Success) throw new IOException("无效适配器身份。");
        for (var i = 0; i < 16; i++)
        {
            var transport = $"usb:{match.Groups[1].Value}:{match.Groups[2].Value}#{i}".ToLowerInvariant();
            var hash = FrameworkCompat.HashPrefix(transport).ToLowerInvariant();
            foreach (var scope in new[] { "Local", "Global" }) try
            {
                if (Mutex.TryOpenExisting(scope + "\\BlueHead-HCI-" + hash, out var mutex))
                { mutex?.Dispose(); throw new IOException("蓝牙接收器仍在使用这个适配器。请先停止接收，再重试。"); }
            }
            catch (UnauthorizedAccessException) { throw new IOException("适配器可能正在被另一个接收器使用，请先关闭接收器。"); }
        }
    }
}
