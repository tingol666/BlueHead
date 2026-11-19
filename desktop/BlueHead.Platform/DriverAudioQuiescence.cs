using System.ComponentModel;
using System.Runtime.InteropServices;
using System.ServiceProcess;

namespace BlueHead.Platform;

public interface IDriverAudioQuiescence
{
    bool SoleWindowsRadio { get; }
    bool GatewayRunning { get; }
    IReadOnlyList<string> ActiveAudioDevices();
    bool Suspend(string instance);
    bool Resume(string instance);
    void StopGateway();
    void StartGateway();
    void Save();
}

/// <summary>Scoped Windows audio retirement. No unpair, persistent disable or audio-service restart.</summary>
public sealed class DriverAudioQuiescence : IDisposable
{
    private readonly DriverRecord record;
    private readonly IDriverAudioQuiescence port;
    private bool disposed;
    public DriverAudioQuiescence(DriverRecord record, IDriverAudioQuiescence port)
    {
        this.record = record; this.port = port;
        record.SuspendedAudioDevices ??= [];
        if (record.SuspendedAudioDevices.Count > 64) throw new IOException("蓝牙声音恢复记录过大，请检查诊断信息。");
        // Recover an interrupted operation before taking a new service lease.
        Restore();
        try
        {
            // BTAG is shared across radios. It may be paused only when every
            // active Windows radio belongs to the adapter being switched.
            if (port.SoleWindowsRadio && port.GatewayRunning)
            {
                record.AudioGatewayWasRunning = true; port.Save();
                port.StopGateway();
            }
            foreach (var instance in port.ActiveAudioDevices())
            {
                if (!record.SuspendedAudioDevices.Contains(instance, StringComparer.OrdinalIgnoreCase))
                { record.SuspendedAudioDevices.Add(instance); port.Save(); }
                if (!port.Suspend(instance))
                { record.SuspendedAudioDevices.Remove(instance); port.Save(); }
            }
        }
        catch { Dispose(); throw; }
    }
    private void Restore()
    {
        var failures = new List<string>();
        // Consumers are retired before their HFP targets and restored after them.
        foreach (var instance in record.SuspendedAudioDevices.AsEnumerable().Reverse().ToArray())
        {
            try
            {
                // False means the old child is absent; its identity remains in
                // the journal and is re-enabled when Windows enumerates it again.
                if (port.Resume(instance)) record.SuspendedAudioDevices.Remove(instance);
            }
            catch (Exception error) when (error is IOException or Win32Exception or InvalidOperationException)
            { failures.Add(error.Message); }
        }
        if (record.AudioGatewayWasRunning)
        {
            try { port.StartGateway(); record.AudioGatewayWasRunning = false; }
            catch (Exception error) when (error is IOException or Win32Exception or InvalidOperationException)
            { failures.Add(error.Message); }
        }
        record.AudioRestoreWarning = failures.Count == 0 ? null : "Windows 蓝牙声音服务恢复未完成，请在蓝牙驱动设置中点击修复。";
        port.Save();
    }
    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        Restore();
    }
}

public static partial class Drivers
{
    private static DriverAudioQuiescence QuiesceDriverAudio(DriverRecord record) => new(record, new WindowsAudioQuiescence(record));
    private sealed class WindowsAudioQuiescence(DriverRecord record) : IDriverAudioQuiescence
    {
        private readonly HashSet<string> sidebandCandidates = new(StringComparer.OrdinalIgnoreCase);
        public bool SoleWindowsRadio => BluetoothRadio.IsSoleSystemRadio(record.InstanceId);
        public bool GatewayRunning
        {
            get
            {
                using var service = new ServiceController("BTAGService");
                try { return service.Status == ServiceControllerStatus.Running; }
                catch (InvalidOperationException error) when (error.InnerException is Win32Exception native && native.NativeErrorCode == 1060) { return false; }
            }
        }
        public void Save() => Drivers.Save(record);
        public void StopGateway()
        {
            using var service = new ServiceController("BTAGService");
            try { service.Stop(); service.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(12)); }
            catch (System.ServiceProcess.TimeoutException) { throw new IOException("Windows 蓝牙通话服务仍在释放连接，请稍候重新检测。"); }
        }
        public void StartGateway()
        {
            using var service = new ServiceController("BTAGService");
            try
            {
                if (service.Status == ServiceControllerStatus.StopPending) service.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(12));
                service.Refresh();
                if (service.Status == ServiceControllerStatus.Stopped) service.Start();
                service.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(12));
            }
            catch (System.ServiceProcess.TimeoutException) { throw new IOException("Windows 蓝牙通话服务尚未恢复，请在蓝牙驱动设置中点击修复。"); }
        }
        private bool Belongs(uint node) => DeviceNative.BelongsTo(node, record.InstanceId);
        public IReadOnlyList<string> ActiveAudioDevices()
        {
            var result = new List<string>();
            var set = DeviceNative.SetupDiGetClassDevs(IntPtr.Zero, "BTHENUM", IntPtr.Zero, DeviceNative.Present | DeviceNative.AllClasses);
            if (set == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                for (uint i = 0; ; i++)
                {
                    var device = DeviceNative.NewDevice();
                    if (!DeviceNative.SetupDiEnumDeviceInfo(set, i, ref device)) break;
                    var id = DeviceNative.InstanceId(set, ref device);
                    if (!AudioProfile(id) || !Belongs(device.Instance)) continue;
                    if (DeviceNative.CM_Get_DevNode_Status(out var state, out var problem, device.Instance, 0) == 0 && problem == 0 && (state & 8) != 0)
                        result.Add(id);
                }
            }
            finally { DeviceNative.SetupDiDestroyDeviceInfoList(set); }
            // A bypass audio driver can keep the HFP interface open even after
            // BTAG has stopped. Only a verified Bluetooth-only component may be
            // retired, and only while the selected radio is the sole Windows
            // radio. Never disable an audio controller, normal output or mic.
            bool bypassPresent = result.Any(id => id.Contains("}_HCIBYPASS_", StringComparison.OrdinalIgnoreCase));
            if (bypassPresent && SoleWindowsRadio)
            {
                var clients = KnownSidebandDevices(startedOnly: true);
                foreach (var client in clients) sidebandCandidates.Add(client);
                result.InsertRange(0, clients);
            }
            return result;
        }
        private static bool AudioProfile(string id) => new[] { "00001108", "00001112", "0000111e", "0000111f", "0000110a", "0000110b" }
            .Any(service => id.StartsWith("BTHENUM\\{" + service + "-0000-1000-8000-00805f9b34fb}", StringComparison.OrdinalIgnoreCase));
        public bool Suspend(string instance)
        {
            if (DeviceNative.CM_Locate_DevNodeW(out var node, instance, 0) != 0) return false;
            bool profile = AudioProfile(instance) && Belongs(node);
            bool sideband = sidebandCandidates.Contains(instance) && SoleWindowsRadio && KnownSidebandDevices(startedOnly: true).Contains(instance, StringComparer.OrdinalIgnoreCase);
            if (!profile && !sideband) return false;
            // CM_DISABLE_UI_NOT_OK only. CM_DISABLE_PERSIST is deliberately absent.
            uint result = DeviceNative.CM_Disable_DevNode(node, 4);
            if (result == 0) return true;
            record.ReleaseDiagnostic = $"Windows 音频子设备暂未释放（CM {result}）。"; Save();
            return false; // Exact device restart below reports a remaining veto.
        }
        public bool Resume(string instance)
        {
            if (DeviceNative.CM_Locate_DevNodeW(out var node, instance, 0) != 0) return false;
            if (AudioProfile(instance))
            {
                if (!Belongs(node)) throw new IOException("蓝牙声音设备已属于其它适配器，未更改它的状态。");
            }
            else if (!KnownSidebandDevices(startedOnly: false).Contains(instance, StringComparer.OrdinalIgnoreCase))
                throw new IOException("蓝牙声音恢复记录与当前设备不匹配，未改动其它声音设备。");
            if (DeviceNative.CM_Get_DevNode_Status(out _, out var problem, node, 0) != 0) return false;
            if (problem != 22) return true; // Only undo our own CM_PROB_DISABLED.
            var result = DeviceNative.CM_Enable_DevNode(node, 0);
            if (result != 0) throw new IOException($"Windows 蓝牙声音设备尚未重新启用（CM {result}）。");
            return true;
        }
        private static List<string> KnownSidebandDevices(bool startedOnly)
        {
            var result = new List<string>();
            var guid = DriverAudioDevicePolicy.MediaClass;
            var set = DeviceNative.SetupDiGetClassDevs(ref guid, null, IntPtr.Zero, DeviceNative.Present);
            if (set == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                for (uint i = 0; ; i++)
                {
                    var device = DeviceNative.NewDevice();
                    if (!DeviceNative.SetupDiEnumDeviceInfo(set, i, ref device)) break;
                    if (!DriverAudioDevicePolicy.BluetoothOnlySideband(device.ClassGuid,
                        DeviceNative.Property(set, ref device, 4), DeviceNative.Property(set, ref device, 1))) continue;
                    if (startedOnly && (DeviceNative.CM_Get_DevNode_Status(out var state, out var problem, device.Instance, 0) != 0 ||
                        problem != 0 || (state & 8) == 0)) continue;
                    result.Add(DeviceNative.InstanceId(set, ref device));
                }
            }
            finally { DeviceNative.SetupDiDestroyDeviceInfoList(set); }
            return result;
        }
    }
    private static void BindAndConfirm(DriverRecord record, string inf, bool dedicated)
    {
        using var audio = QuiesceDriverAudio(record);
        record.ReleaseDiagnostic = null;
        record.RebootRequired = Bind(record.InstanceId, inf);
        record.SystemRadioReady = false;
        WaitForDriverReady(record, dedicated);
    }
}
