using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace BlueHead.Platform;

/// <summary>Read readiness of this physical adapter, never another installed radio.</summary>
internal static class BluetoothRadio
{
    private static readonly Guid RadioInterface = new("0850302A-B344-4fda-9BE9-90576B8D46F0");
    [StructLayout(LayoutKind.Sequential)] private struct InterfaceData
    { public uint Size; public Guid ClassGuid; public uint Flags; public UIntPtr Reserved; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct RadioInfo
    {
        public uint Size; public ulong Address;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 248)] public string Name;
        public uint Class; public ushort Subversion, Manufacturer;
    }
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr SetupDiGetClassDevs(ref Guid guid, string? enumerator, IntPtr window, uint flags);
    [DllImport("setupapi.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SetupDiEnumDeviceInterfaces(IntPtr set, IntPtr device, ref Guid guid, uint index, ref InterfaceData data);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SetupDiGetDeviceInterfaceDetail(IntPtr set, ref InterfaceData data, IntPtr detail, int size, out int required, ref DeviceNative.DeviceInfo device);
    [DllImport("cfgmgr32.dll")] private static extern uint CM_Get_Parent(out uint parent, uint instance, uint flags);
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] private static extern uint CM_Get_Device_ID(uint instance, StringBuilder id, int length, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("bthprops.cpl")] private static extern uint BluetoothGetRadioInfo(SafeFileHandle radio, ref RadioInfo info);

    private static bool Matches(uint node, string instance)
    {
        for (int i = 0; i < 8; i++)
        {
            var id = new StringBuilder(1024);
            if (CM_Get_Device_ID(node, id, id.Capacity, 0) != 0) return false;
            if (id.ToString().Equals(instance, StringComparison.OrdinalIgnoreCase)) return true;
            if (CM_Get_Parent(out var parent, node, 0) != 0) return false;
            node = parent;
        }
        return false;
    }
    public static bool IsReady(string instance) => Query(instance);
    public static bool AnyReady() => Query(null);
    private static bool Query(string? instance)
    {
        var guid = RadioInterface;
        var set = SetupDiGetClassDevs(ref guid, null, IntPtr.Zero, 2 | 16); // PRESENT | DEVICEINTERFACE
        if (set == new IntPtr(-1)) return false;
        try
        {
            for (uint i = 0; ; i++)
            {
                var data = new InterfaceData { Size = (uint)Marshal.SizeOf<InterfaceData>() };
                if (!SetupDiEnumDeviceInterfaces(set, IntPtr.Zero, ref guid, i, ref data)) break;
                var device = DeviceNative.NewDevice();
                SetupDiGetDeviceInterfaceDetail(set, ref data, IntPtr.Zero, 0, out var required, ref device);
                if (required < 8 || required > 65536) continue;
                var memory = Marshal.AllocHGlobal(required);
                try
                {
                    Marshal.WriteInt32(memory, IntPtr.Size == 8 ? 8 : 6);
                    if (!SetupDiGetDeviceInterfaceDetail(set, ref data, memory, required, out _, ref device) || (instance != null && !Matches(device.Instance, instance))) continue;
                    var path = Marshal.PtrToStringUni(IntPtr.Add(memory, 4));
                    if (string.IsNullOrEmpty(path)) continue;
                    using var radio = CreateFile(path!, 0, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
                    var info = new RadioInfo { Size = (uint)Marshal.SizeOf<RadioInfo>(), Name = "" };
                    if (!radio.IsInvalid && BluetoothGetRadioInfo(radio, ref info) == 0 && DriverReadiness.ValidRadioAddress(info.Address)) return true;
                }
                finally { Marshal.FreeHGlobal(memory); }
            }
        }
        finally { DeviceNative.SetupDiDestroyDeviceInfoList(set); }
        return false;
    }
}
