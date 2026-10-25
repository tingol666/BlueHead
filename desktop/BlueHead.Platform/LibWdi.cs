using System.ComponentModel;
using System.Runtime.InteropServices;

namespace BlueHead.Platform;

internal static class LibWdi
{
    [StructLayout(LayoutKind.Sequential)] private struct Device
    {
        public IntPtr Next; public ushort Vid, Pid; public int Composite; public byte Interface;
        public IntPtr Description, Driver, DeviceId, HardwareId, CompatibleId, UpperFilter;
        public ulong Version;
    }
    [StructLayout(LayoutKind.Sequential)] private struct ListOptions { public int All, Hubs, Trim; }
    [StructLayout(LayoutKind.Sequential)] private struct PrepareOptions
    {
        public int Type; public IntPtr Vendor, Guid; public int DisableCatalog, DisableSigning;
        public IntPtr Subject; public int Wcid, External;
    }
    [DllImport("libwdi.dll")] private static extern int wdi_create_list(out IntPtr list, ref ListOptions options);
    [DllImport("libwdi.dll")] private static extern int wdi_destroy_list(IntPtr list);
    [DllImport("libwdi.dll", CharSet = CharSet.Ansi)] private static extern int wdi_prepare_driver(IntPtr device, [MarshalAs(UnmanagedType.LPUTF8Str)] string path, [MarshalAs(UnmanagedType.LPUTF8Str)] string inf, ref PrepareOptions options);
    [DllImport("libwdi.dll")] private static extern IntPtr wdi_strerror(int code);
    internal static void Prepare(string instanceId, string folder)
    {
        var options = new ListOptions { All = 1, Trim = 1 };
        var error = wdi_create_list(out var list, ref options);
        Check(error);
        try
        {
            for (var pointer = list; pointer != IntPtr.Zero;)
            {
                var device = Marshal.PtrToStructure<Device>(pointer);
                if (Marshal.PtrToStringUTF8(device.DeviceId)?.Equals(instanceId, StringComparison.OrdinalIgnoreCase) == true)
                {
                    var prepare = new PrepareOptions { Type = 0 };
                    Check(wdi_prepare_driver(pointer, folder, "bluehead.inf", ref prepare));
                    if (!File.Exists(Path.Combine(folder, "bluehead.inf")) || !File.Exists(Path.Combine(folder, "bluehead.cat")))
                        throw new IOException("驱动包生成不完整。");
                    return;
                }
                pointer = device.Next;
            }
            throw new IOException("驱动准备期间所选适配器已断开。");
        }
        finally { wdi_destroy_list(list); }
    }
    private static void Check(int code)
    {
        if (code != 0) throw new Win32Exception($"驱动准备失败：{Marshal.PtrToStringUTF8(wdi_strerror(code))} ({code})");
    }
}
