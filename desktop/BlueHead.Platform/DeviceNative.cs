using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32;

namespace BlueHead.Platform;

internal static class DeviceNative
{
    internal const uint Present = 2, AllClasses = 4, CompatibleDriver = 2;
    [StructLayout(LayoutKind.Sequential)] internal struct DeviceInfo
    { public uint Size; public Guid ClassGuid; public uint Instance; public IntPtr Reserved; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct DriverInfo
    {
        public uint Size, Type; public UIntPtr Reserved;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Description;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Manufacturer;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Provider;
        public System.Runtime.InteropServices.ComTypes.FILETIME Date; public ulong Version;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct DriverDetail
    {
        public uint Size; public System.Runtime.InteropServices.ComTypes.FILETIME Date;
        public uint CompatibleOffset, CompatibleLength; public UIntPtr Reserved;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Section;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string Inf;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Description;
        public char HardwareId;
    }
    [StructLayout(LayoutKind.Sequential)] internal struct PropertyChange
    { public uint HeaderSize, Function, State, Scope, HardwareProfile; }
    [StructLayout(LayoutKind.Sequential)] internal struct DriverInstallParameters
    { public uint Size, Rank, Flags; public UIntPtr PrivateData; public uint Reserved; }
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool SetupDiGetDriverInstallParams(IntPtr set, ref DeviceInfo device, ref DriverInfo driver, ref DriverInstallParameters parameters);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SetupDiGetINFClass(string path, out Guid guid, StringBuilder name, int length, out int required);
    internal static bool IsBluetoothInf(string path) => SetupDiGetINFClass(path, out var guid, new StringBuilder(256), 256, out _) && guid == new Guid("e0cbf06c-cd8b-4647-bb8a-263b43f0f974");
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct InstallParameters
    {
        public uint Size, Flags, FlagsEx;
        public IntPtr Window, Callback, Context, FileQueue;
        public UIntPtr ClassReserved; public uint Reserved;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string DriverPath;
    }
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern IntPtr SetupDiGetClassDevs(IntPtr guid, string? enumerator, IntPtr window, uint flags);
    [DllImport("setupapi.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool SetupDiEnumDeviceInfo(IntPtr set, uint index, ref DeviceInfo device);
    [DllImport("setupapi.dll", SetLastError = true)] internal static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern bool SetupDiGetDeviceInstanceId(IntPtr set, ref DeviceInfo device, StringBuilder buffer, int length, out int required);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern bool SetupDiGetDeviceRegistryProperty(IntPtr set, ref DeviceInfo device, uint property, out uint type, byte[] buffer, int size, out int required);
    [DllImport("setupapi.dll", SetLastError = true)] internal static extern bool SetupDiBuildDriverInfoList(IntPtr set, ref DeviceInfo device, uint type);
    [DllImport("setupapi.dll", SetLastError = true)] internal static extern bool SetupDiDestroyDriverInfoList(IntPtr set, ref DeviceInfo device, uint type);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern bool SetupDiEnumDriverInfo(IntPtr set, ref DeviceInfo device, uint type, uint index, ref DriverInfo driver);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern bool SetupDiGetDriverInfoDetail(IntPtr set, ref DeviceInfo device, ref DriverInfo driver, IntPtr detail, int length, out int required);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern bool SetupCopyOEMInf(string source, string? sourcePath, uint media, uint style, StringBuilder destination, int length, out int required, IntPtr component);
    [DllImport("newdev.dll", SetLastError = true)] internal static extern bool DiInstallDevice(IntPtr window, IntPtr set, ref DeviceInfo device, ref DriverInfo driver, uint flags, out bool reboot);
    [DllImport("cfgmgr32.dll")] internal static extern uint CM_Get_DevNode_Status(out uint status, out uint problem, uint instance, uint flags);
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] private static extern uint CM_Get_Device_ID(uint instance, StringBuilder id, int length, uint flags);
    [DllImport("cfgmgr32.dll")] private static extern uint CM_Get_Parent(out uint parent, uint instance, uint flags);
    internal static string PhysicalInstance(uint node)
    {
        for (var depth = 0; depth < 8; depth++)
        {
            var id = new StringBuilder(1024);
            if (CM_Get_Device_ID(node, id, id.Capacity, 0) != 0) break;
            var value = id.ToString().ToUpperInvariant();
            if (value.StartsWith(@"USB\VID_", StringComparison.Ordinal) && !value.Contains("&MI_")) return value;
            if (CM_Get_Parent(out var parent, node, 0) != 0) break;
            node = parent;
        }
        return "";
    }
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool SetupDiSetClassInstallParams(IntPtr set, ref DeviceInfo device, ref PropertyChange parameters, int size);
    [DllImport("setupapi.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool SetupDiCallClassInstaller(uint function, IntPtr set, ref DeviceInfo device);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool SetupDiGetDeviceInstallParams(IntPtr set, ref DeviceInfo device, ref InstallParameters parameters);
    internal static DeviceInfo NewDevice() => new() { Size = (uint)Marshal.SizeOf<DeviceInfo>() };
    internal static DriverInfo NewDriver() => new() { Size = (uint)Marshal.SizeOf<DriverInfo>(), Description = "", Manufacturer = "", Provider = "" };
    internal static string Property(IntPtr set, ref DeviceInfo device, uint property)
    {
        var data = new byte[8192];
        return SetupDiGetDeviceRegistryProperty(set, ref device, property, out _, data, data.Length, out var required)
            ? Encoding.Unicode.GetString(data, 0, Math.Min(required, data.Length)).TrimEnd('\0') : "";
    }
    internal static string InstanceId(IntPtr set, ref DeviceInfo device)
    {
        var value = new StringBuilder(1024);
        return SetupDiGetDeviceInstanceId(set, ref device, value, value.Capacity, out _) ? value.ToString() : "";
    }
    internal static string DriverInf(IntPtr set, ref DeviceInfo device)
    {
        var key = Property(set, ref device, 9);
        if (key.Length == 0) return "";
        using var driver = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Control\Class\" + key);
        return driver?.GetValue("InfPath")?.ToString() ?? "";
    }
    internal static string DetailInf(IntPtr set, ref DeviceInfo device, ref DriverInfo driver)
    {
        var memory = Marshal.AllocHGlobal(65536);
        try
        {
            Marshal.WriteInt32(memory, Marshal.SizeOf<DriverDetail>());
            if (!SetupDiGetDriverInfoDetail(set, ref device, ref driver, memory, 65536, out _)) throw new Win32Exception(Marshal.GetLastWin32Error());
            return Marshal.PtrToStructure<DriverDetail>(memory).Inf;
        }
        finally { Marshal.FreeHGlobal(memory); }
    }
}
