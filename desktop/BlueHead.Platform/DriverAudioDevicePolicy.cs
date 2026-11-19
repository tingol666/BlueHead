namespace BlueHead.Platform;

/// <summary>Known Bluetooth-only sideband clients; never infer ownership from a display name.</summary>
public static class DriverAudioDevicePolicy
{
    public static readonly Guid MediaClass = new("4d36e96c-e325-11ce-bfc1-08002be10318");

    public static bool BluetoothOnlySideband(Guid deviceClass, string service, string hardwareIds) =>
        deviceClass == MediaClass && service.Equals("IntcBTAu", StringComparison.OrdinalIgnoreCase) &&
        hardwareIds.Split('\0').Any(id => id.StartsWith("INTELAUDIO\\", StringComparison.OrdinalIgnoreCase) &&
            (id.Contains("&LINKTYPE_03&", StringComparison.OrdinalIgnoreCase) || id.EndsWith("&LINKTYPE_03", StringComparison.OrdinalIgnoreCase)));

    public static bool CanRetireSideband(bool soleSelectedWindowsRadio, bool selectedBypassPresent,
        Guid deviceClass, string service, string hardwareIds) => soleSelectedWindowsRadio && selectedBypassPresent &&
        BluetoothOnlySideband(deviceClass, service, hardwareIds);
}
