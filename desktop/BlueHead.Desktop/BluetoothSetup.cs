using BlueHead.Platform;

namespace BlueHead.Desktop;

// Pure presentation policy shared by the main and independent recovery views.
// No driver writes, radio access, UI objects or saved-setting side effects.
internal enum BluetoothSetupStage { Checking, Ready, SelectAdapter, DedicatedRequired, SystemRestoreRequired, SystemBluetoothRequired, RestartRequired, Unavailable, CheckFailed, InitializationRequired }
internal sealed record ReceptionGuide(BluetoothSetupStage Stage, string Title, string Detail, string Action)
{
    public bool Ready => Stage == BluetoothSetupStage.Ready;
}
internal sealed record AdapterGuide(string State, string Detail, bool CanInstall, bool CanRestore, string InstallLabel, string RestoreLabel);
internal sealed record ControllerGuide(string Title, string Detail, bool CanAcquire, bool CanOriginal, bool CanRetry);
internal static class BluetoothSetup
{
    public static ControllerGuide Controller(string code, string profile)
    {
        bool known = profile.Length is > 0 and <= 80 && profile.All(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.');
        if (code == "unsupported_capability")
            return new("此适配器不支持经典蓝牙音频", "专用接收需要经典蓝牙 BR/EDR。请改用支持经典音频的适配器。", false, false, false);
        if (code is "missing_firmware" or "corrupt_firmware" or "source_unavailable")
            return new("需要准备蓝牙启动固件", "可在线获取并验证匹配文件，或从原厂驱动包提取。完成后重新开始接收。", known, true, true);
        if (code is "catalog_untrusted" or "invalid_firmware")
            return new("固件校验未通过", "请重新安装完整的官方 BlueHead 包。不会加载未经验证的文件；也可恢复系统蓝牙。", false, false, false);
        return new("蓝牙控制器尚未就绪", "先检查连接并重试。仍不能启动时，可让原厂驱动初始化，再验证专用接收；失败会恢复系统蓝牙。", false, true, true);
    }
    public static bool Interrupted(string stage) => stage is "preparing_winusb" or "binding_winusb" or "restoring" or
        "initializing_before_restore" or "bootstrap_restoring_original" or "bootstrap_original_ready" or "bootstrap_binding_winusb" or "bootstrap_returning_to_windows" or "bootstrap_failed" or "recovery_required";
    public static bool Dedicated(DriverDevice? device) => string.Equals(device?.Service, "WinUSB", StringComparison.OrdinalIgnoreCase);
    public static ReceptionGuide Reception(bool shared, bool inspected, bool failed, bool transportPresent, DriverDevice? target, bool systemRadioReady, bool restorableDedicated)
    {
        if (failed) return new(BluetoothSetupStage.CheckFailed, "蓝牙状态检查未完成", "请刷新状态后继续；不会自动更换驱动。", "重新检查");
        if (!inspected) return new(BluetoothSetupStage.Checking, "正在检查蓝牙", "正在确认接收方式与适配器状态…", "正在检查…");
        if (shared)
        {
            if (systemRadioReady) return new(BluetoothSetupStage.Ready, "系统蓝牙已就绪", "先在 Windows 中配对设备，再开始接收媒体。电脑麦克风与混音需专用接收。", "开始接收");
            if (target is { RebootRequired: true } && !Dedicated(target)) return Restart();
            if (restorableDedicated) return new(BluetoothSetupStage.SystemRestoreRequired, "先恢复系统蓝牙", "适配器目前由 BlueHead 专用。恢复给 Windows 后，才能与耳机、键鼠等设备共用。", "恢复系统蓝牙");
            return new(BluetoothSetupStage.SystemBluetoothRequired, "打开 Windows 蓝牙", "尚未发现可用的系统蓝牙。请在 Windows 打开蓝牙并完成设备配对，然后刷新状态。", "打开 Windows 蓝牙");
        }
        if (target?.RebootRequired == true) return Restart();
        if (target != null && !Dedicated(target)) return new(BluetoothSetupStage.DedicatedRequired, "先启用专用接收", "当前适配器由 Windows 管理。声音与麦克风、编码选择和多设备混音需要一个专用适配器。", "启用专用接收");
        if (target is { Healthy: false }) return new(BluetoothSetupStage.Unavailable, "接收适配器尚不可用", "请检查适配器连接或 Windows 设备状态，再刷新。原驱动备份会保留。", "检查蓝牙设置");
        if (!transportPresent) return new(BluetoothSetupStage.SelectAdapter, "选择接收适配器", "需要把一个内置或外置蓝牙适配器交给 BlueHead，或改用系统共用方式收听媒体。", "设置蓝牙接收");
        return new(BluetoothSetupStage.Ready, "专用接收已就绪", "开始接收时自动检查并初始化固件，再连接移动设备。", "开始接收");
    }
    private static ReceptionGuide Restart() => new(BluetoothSetupStage.RestartRequired, "需要重启 Windows", "当前驱动操作尚需重启电脑完成初始化。配对、音量与原厂驱动备份会保留。", "查看重启要求");
    public static AdapterGuide Adapter(DriverDevice? device)
    {
        if (device == null) return new("未选择适配器", "选择要交给 BlueHead 或恢复给 Windows 的蓝牙适配器。", false, false, "启用专用接收", "恢复系统蓝牙");
        if (Dedicated(device))
            return new(device.RebootRequired ? "专用驱动已启用 · 需要重启" : device.Healthy ? "此适配器由 BlueHead 专用" : "专用适配器尚不可用",
                device.RebootRequired ? "请重启电脑完成准备；也可先恢复为系统蓝牙。" : device.Healthy ? "开始接收时自动初始化固件。要与 Windows 共用，请先恢复系统蓝牙。" : "检查适配器连接与设备状态；不会把驱动重复安装当作固件初始化。",
                false, device.HasBackup, "专用接收已启用", device.HasBackup ? "恢复系统蓝牙" : "没有可用恢复备份");
        if (device.RebootRequired)
            return new("系统驱动已恢复 · 请重启电脑", "驱动恢复完成，请重启电脑。无需再次恢复或重新启用专用接收。", false, false, "重启后可启用专用接收", "已恢复 · 等待重启");
        if (device.Healthy)
            return new(device.SystemRadioReady == false ? "由 Windows 管理 · 蓝牙尚未就绪" : "此适配器由 Windows 管理",
                device.SystemRadioReady == false ? "驱动已在系统模式。请在 Windows 打开蓝牙并检查设备状态；若仍不能初始化，请重启电脑。" : "可与 Windows 蓝牙设备共用。需要电脑麦克风与混音时，再启用专用接收。",
                true, false, "启用专用接收", "已在使用系统蓝牙");
        return new("系统适配器需要检查", "先检查连接与 Windows 设备状态；有原厂备份时可恢复并重新初始化。", false, device.HasBackup, "适配器尚不可用", device.HasBackup ? "恢复并初始化系统蓝牙" : "没有可用恢复备份");
    }
}
