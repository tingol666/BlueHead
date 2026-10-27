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
internal sealed record DriverFeedback(bool Completed, bool Ready, string EffectiveOperation, string Message);
internal sealed record UsageChangeGuide(string Title, string Instruction, string ImpactTitle, string Impact, bool Critical);
internal sealed record DriverActivityGuide(string Title, string Detail);
internal static class BluetoothSetup
{
    public static string UsageName(string mode) => mode == "native" ? "Windows 共用蓝牙" : "BlueHead 专用蓝牙";
    public static UsageChangeGuide UsageChange(string mode) => mode == "native"
        ? new("待你确认：恢复 Windows 共用蓝牙", "尚未更换驱动。请在本页底部右侧点击“恢复 Windows 蓝牙”；返回或取消不会改动。",
            "恢复后：系统蓝牙可用，BlueHead 改用基础功能",
            "Windows 蓝牙开关、蓝牙键盘、鼠标和耳机功能可以重新使用。BlueHead 将只接收音乐／视频，不再通过此适配器发送电脑麦克风或选择媒体编码。若仍需重启，页面会明确提示。", false)
        : new("待你确认：改用 BlueHead 专用蓝牙", "尚未更换驱动。请在本页底部右侧点击“备份并启用专用蓝牙”；返回或取消不会改动。",
            "切换后：此蓝牙连接的键盘、鼠标和耳机会断开",
            "若没有其它系统蓝牙，Windows 设置中的蓝牙开关会消失。需要保留键盘、鼠标／耳机连接请用共用方式，或另用一个适配器。程序会先备份原驱动，以后可点击右下角“恢复 Windows 蓝牙”切回。", true);
    public static DriverActivityGuide DriverActivity(string operation, bool awaitingPermission)
    {
        if (awaitingPermission) return new("等待 Windows 管理员授权", "请在 Windows 的授权弹窗中选择“是”。选择“否”会取消，本次不会执行驱动操作。");
        return operation switch
        {
            "install" => new("正在备份原驱动并启用专用蓝牙", "正在把所选蓝牙交给 BlueHead。请等待完成，不要拔出蓝牙设备或关闭电脑。"),
            "restore" => new("正在恢复 Windows 蓝牙驱动", "正在把所选蓝牙交还 Windows 并检查能否使用。请等待完成，不要拔出蓝牙设备或关闭电脑。"),
            _ => new("正在修复 BlueHead 专用蓝牙", "正在使用原厂驱动初始化蓝牙，再返回专用方式。请等待完成；若仍需重启，会明确提示。")
        };
    }
    public static ControllerGuide Controller(string code, string profile)
    {
        bool known = profile.Length is > 0 and <= 80 && profile.All(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.');
        if (code == "unsupported_capability")
            return new("此适配器不支持经典蓝牙音频", "专用接收需要经典蓝牙 BR/EDR。请改用支持经典音频的适配器。", false, false, false);
        if (code is "missing_firmware" or "corrupt_firmware" or "source_unavailable")
            return new("需要准备蓝牙启动固件", "可在线获取匹配文件，或从原厂驱动包提取。校验后保存在本机，之后无需联网。", known, true, true);
        if (code is "catalog_untrusted" or "invalid_firmware")
            return new("固件校验未通过", "请重新安装完整的官方 BlueHead 包。不会加载未经验证的文件；也可恢复系统蓝牙。", false, false, false);
        if (code is "controller_unresponsive" or "controller_hardware_error" or "controller_restarted" or "controller_connection_lost")
            return new("蓝牙暂时不可用", "接收已停止。点击此提示右侧“处理蓝牙问题”，再在页面底部右侧点击“修复专用蓝牙”。由你确认后才会请求管理员授权；完成后回音频中心点击“开始接收”。", false, true, true);
        return new("蓝牙启动失败", "请先重试。仍然失败时，请打开左侧“蓝牙驱动设置”，在页面底部右侧点击“修复专用蓝牙”；需要重启电脑时会明确提示。", false, true, true);
    }
    public static DriverFeedback DriverCompletion(string operation, int exitCode, DriverRecord? record)
    {
        string failure = DriverFailure(operation, record?.Error ?? "");
        if (exitCode is not (0 or 3010) || record == null) return new(false, false, operation, failure);
        bool dedicated = record.Stage is "winusb_ready" or "winusb_restart_required";
        bool system = record.Stage is "restored" or "restored_restart_required";
        if (!(operation == "initialize-original" ? dedicated || system : operation == "install" ? dedicated : system))
            return new(false, false, operation, failure);
        string effective = dedicated ? "install" : "restore";
        if (record.RebootRequired || exitCode == 3010)
            return new(true, false, effective, effective == "restore" ? "驱动恢复完成，请重启电脑。" : "蓝牙已切换，请重启电脑后开始接收。");
        if (operation == "initialize-original" && !dedicated)
            return new(true, false, effective, "已恢复系统蓝牙，本次未恢复声音和麦克风。可先使用基础模式收听媒体，或稍后在本页底部右侧点击“备份并启用专用蓝牙”。");
        return new(true, true, effective, operation == "initialize-original" ? "专用蓝牙已修复。回音频中心点击“开始接收”，再在手机／平板上重新连接。" : dedicated ? "进阶模式已准备好。回音频中心点击“开始接收”，再在手机／平板的蓝牙列表中连接电脑。" : "Windows 蓝牙已恢复，基础模式已生效。请在电脑的 Windows 蓝牙设置中配对手机／平板，再回音频中心点击“开始接收”。");
    }
    public static bool ModeAvailable(string mode, DriverDevice? device, bool systemReady)
    {
        if (device?.RebootRequired == true || device is { Healthy: false }) return false;
        return mode == "native" ? systemReady && (device == null || !Dedicated(device)) : mode == "advanced" && Dedicated(device) && device?.Healthy == true;
    }
    public static string DriverFailure(string operation, string detail)
    {
        string action = operation == "initialize-original" ? "专用接收修复未完成" : operation == "restore" ? "Windows 蓝牙恢复未完成" : "专用驱动切换未完成";
        string normalized = detail.ToLowerInvariant();
        if (normalized.Contains("busy") || normalized.Contains("in use") || normalized.Contains("占用") || normalized.Contains("0x000000aa") || normalized.Contains("0x800700aa") || normalized.Contains("sharing violation"))
            return action + "：蓝牙被其它程序占用。先停止接收并关闭其它蓝牙接收软件，再点“刷新状态”后重试；仍被占用时，重启电脑后重试。";
        if (normalized.Contains("backup") || normalized.Contains("备份"))
            return action + "：请检查原厂驱动备份。没有可用备份时，安装电脑或蓝牙设备厂商提供的原厂驱动；可使用下方独立恢复工具。";
        if (normalized.Contains("denied") || normalized.Contains("拒绝访问") || normalized.Contains("权限"))
            return action + "：没有获得更改驱动的权限。重新点击对应操作，并允许 Windows 的管理员授权；仍失败时，重启电脑后重试。";
        return action + "。先点“刷新状态”查看驱动现状，再重试对应操作；仍不能完成时，重启电脑后重试。配对和原厂驱动备份会保留。";
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
            if (systemRadioReady) return new(BluetoothSetupStage.Ready, "系统蓝牙已就绪", "在电脑打开 Windows 蓝牙设置，添加并配对手机／平板；回音频中心点击“开始接收”，然后在手机／平板播放音乐或视频。", "开始接收");
            if (target is { RebootRequired: true } && !Dedicated(target)) return Restart();
            if (restorableDedicated) return new(BluetoothSetupStage.SystemRestoreRequired, "需要切换接收方式", "先打开左侧“蓝牙驱动设置”，在页面底部右侧点击“恢复 Windows 蓝牙”。恢复后，在电脑的 Windows 蓝牙设置中配对手机／平板，再回音频中心点击“开始接收”。", "设置基础模式");
            return new(BluetoothSetupStage.SystemBluetoothRequired, "打开 Windows 蓝牙", "请点击“打开 Windows 蓝牙”，在电脑的 Windows 设置中打开蓝牙，点击“添加设备”→“蓝牙”配对手机／平板。完成后回 BlueHead 点击“刷新状态”。", "打开 Windows 蓝牙");
        }
        if (target?.RebootRequired == true) return Restart();
        if (target != null && !Dedicated(target)) return new(BluetoothSetupStage.DedicatedRequired, "需要切换接收方式", "请打开左侧“蓝牙驱动设置”，阅读影响后，在页面底部右侧点击“备份并启用专用蓝牙”。该蓝牙设备连接的鼠标、键盘和耳机会断开；以后可恢复 Windows 蓝牙。", "设置进阶模式");
        if (target is { Healthy: false }) return new(BluetoothSetupStage.Unavailable, "接收适配器尚不可用", "请检查适配器连接或 Windows 设备状态，再刷新。原驱动备份会保留。", "检查蓝牙设置");
        if (!transportPresent) return new(BluetoothSetupStage.SelectAdapter, "选择接收适配器", "进阶模式需要一个电脑蓝牙设备使用专用驱动。请打开蓝牙驱动设置准备，或在上方改选基础模式，只接收音乐、视频声音。", "打开蓝牙驱动设置");
        return new(BluetoothSetupStage.Ready, "专用接收已就绪", "开始接收时自动检查并初始化固件，再连接移动设备。", "开始接收");
    }
    private static ReceptionGuide Restart() => new(BluetoothSetupStage.RestartRequired, "需要重启 Windows", "当前驱动操作尚需重启电脑完成初始化。配对、音量与原厂驱动备份会保留。", "查看重启要求");
    public static AdapterGuide Adapter(DriverDevice? device)
    {
        if (device == null) return new("未选择适配器", "请在上方选择电脑的蓝牙设备。没有列出时，插入 USB 蓝牙适配器后点击“刷新状态”。", false, false, "启用专用接收", "设置基础模式");
        if (Dedicated(device))
            return new(device.RebootRequired ? "专用驱动已启用 · 需要重启" : device.Healthy ? "此适配器由 BlueHead 专用" : "专用适配器尚不可用",
                device.RebootRequired ? "请重启电脑完成准备；需要使用系统蓝牙时可恢复 Windows 驱动。" : device.Healthy ? "专用驱动已启用，可用于进阶模式。在本页底部右侧：需要连接 Windows 蓝牙键盘、鼠标／耳机时，点“恢复 Windows 蓝牙”；无法开始使用时，点“修复专用蓝牙”。" : "请确认蓝牙设备已插好，点击“修复专用蓝牙”后重试；仍然失败时恢复 Windows 蓝牙。",
                false, device.HasBackup, "专用接收已启用", device.HasBackup ? "恢复系统蓝牙" : "没有可用恢复备份");
        if (device.RebootRequired)
            return new("系统驱动已恢复 · 请重启电脑", "驱动恢复完成，请重启电脑。无需再次恢复或重新启用专用接收。", false, false, "重启后可启用专用接收", "已恢复 · 等待重启");
        if (device.Healthy)
            return new(device.SystemRadioReady == false ? "由 Windows 管理 · 蓝牙尚未就绪" : "此适配器由 Windows 管理",
                device.SystemRadioReady == false ? "驱动已在系统模式。请在 Windows 打开蓝牙并检查设备状态；若仍不能初始化，请重启电脑。" : "正在使用 Windows 驱动，可以保留蓝牙鼠标、键盘和耳机。基础模式无需修改驱动；进阶模式需要备份并切换专用驱动。",
                true, false, "启用专用接收", "已在使用系统蓝牙");
        return new("系统适配器需要检查", "先检查连接与 Windows 设备状态；有原厂备份时可恢复并重新初始化。", false, device.HasBackup, "适配器尚不可用", device.HasBackup ? "恢复并初始化系统蓝牙" : "没有可用恢复备份");
    }
}
