using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;
using BlueHead.Platform;
using Microsoft.Win32;

namespace BlueHead.Desktop;

public partial class MainWindow
{
    private string? controllerFailureId, controllerCode, controllerProfile, controllerDiagnostic;
    private bool sessionMediaOnly, controllerNotice;
    private string? driverDiagnostic;
    private CancellationTokenSource? firmwareCancellation;
    private AdapterChoice LabelAdapter(AdapterChoice choice)
    {
        var matches = Drivers.MatchTransport(choice.Transport, driverInventory);
        return matches.Count == 1 ? choice with { Name = matches[0].DeviceLabel } : choice;
    }
    internal ControllerGuide? CurrentControllerGuide => settings.mode != "native" && ReceptionAdapter is { } adapter &&
        adapter.AdapterId == controllerFailureId && controllerCode != null ? BluetoothSetup.Controller(controllerCode, controllerProfile ?? "") : null;
    internal bool ControllerNeedsInitialization => CurrentControllerGuide != null && ReceptionAdapter?.HasBackup == true &&
        controllerCode is "controller_unresponsive" or "controller_hardware_error" or "controller_restarted" or "controller_connection_lost";
    private ControllerGuide? SelectedControllerGuide => DriverPicker.SelectedItem is DriverDevice adapter &&
        adapter.AdapterId == controllerFailureId && controllerCode != null ? BluetoothSetup.Controller(controllerCode, controllerProfile ?? "") : null;
    private bool ApplyControllerFailure(JsonElement message)
    {
        string code = message.Text("category");
        if (code.Length == 0 || code.StartsWith("audio_", StringComparison.Ordinal) ||
            code is "source_expired" or "native_channel" or "session_cleanup" or "usb_callback_failed" or "device_removed" || ReceptionAdapter == null) return false;
        controllerFailureId = ReceptionAdapter.AdapterId;
        controllerCode = code; controllerProfile = message.Text("firmware_profile"); controllerDiagnostic = message.Text("message");
        var guide = BluetoothSetup.Controller(code, controllerProfile);
        ControllerPreparation.IsExpanded = guide.CanAcquire;
        ShowNotice(guide.Title + "。" + guide.Detail); controllerNotice = true;
        UpdateControllerActions(); UpdateSetupState();
        return true;
    }
    private void ClearControllerFailure()
    {
        if (controllerNotice) { Notice.Visibility = Visibility.Collapsed; controllerNotice = false; }
        controllerFailureId = controllerCode = controllerProfile = controllerDiagnostic = null;
        UpdateControllerActions(); UpdateSetupState();
    }
    private void UpdateControllerActions()
    {
        if (ControllerPreparation == null) return;
        var device = DriverPicker.SelectedItem as DriverDevice;
        var guide = SelectedControllerGuide;
        bool dedicated = device != null && BluetoothSetup.Dedicated(device);
        bool available = !running && !stopping && !starting && !driverBusy && !driverChecking && !closing && device?.RebootRequired == false && dedicated;
        ControllerPreparation.Visibility = dedicated && guide?.CanAcquire == true ? Visibility.Visible : Visibility.Collapsed;
        ControllerTitle.Text = guide?.Title ?? "蓝牙启动文件";
        ControllerDetail.Text = guide?.Detail ?? "正常使用时无需操作。只有页面提示缺少启动文件时，才需获取或导入。";
        AcquireFirmwareButton.IsEnabled = ImportFirmwareButton.IsEnabled = available && guide?.CanAcquire == true;
        RetryControllerButton.IsEnabled = available && (guide?.CanRetry ?? true);
        AcquireFirmwareButton.Visibility = ImportFirmwareButton.Visibility = guide?.CanAcquire == true ? Visibility.Visible : Visibility.Collapsed;
        CancelFirmwareButton.Visibility = firmwareCancellation != null ? Visibility.Visible : Visibility.Collapsed;
        DriverNativeDetails.Text = device == null ? "未检测到可配置的 USB 蓝牙适配器。" : $"驱动服务：{device.Service}\n驱动文件：{device.Inf}";
        if (device != null) DriverNativeDetails.Text += device.HasBackup ? "\n原厂驱动备份已保留。" : "\n尚无原厂驱动备份。";
        if (device != null && device.AdapterId == controllerFailureId && controllerDiagnostic != null)
            DriverNativeDetails.Text += "\n启动详情：" + controllerDiagnostic;
        if (device != null && device.InstanceId == driverResultInstance && driverDiagnostic != null)
            DriverNativeDetails.Text += "\n恢复详情：" + driverDiagnostic;
        if (guide != null && dedicated) { DriverStateTitle.Text = "3  " + guide.Title; DriverStatus.Text = guide.CanAcquire ? "在“需要准备蓝牙启动文件”区域点击“自动获取启动文件”，或“从原厂驱动包提取”。完成后回音频中心点击“开始接收”。" : "此设备仍是专用驱动，但蓝牙没有正常响应。在本页底部右侧点击“修复专用蓝牙”，并允许管理员授权；完成后回音频中心点击“开始接收”。"; }
    }
    private static string RuntimeTool()
    {
        string installed = Path.Combine(AppContext.BaseDirectory, "engine", "BlueHead.Runtime.exe");
        if (File.Exists(installed)) return installed;
        string? source = Environment.GetEnvironmentVariable("BLUEHEAD_SOURCE_ROOT");
        string candidate = source == null ? "" : Path.Combine(source, "bluehead", "assets", "BlueHead.Runtime.exe");
        return File.Exists(candidate) ? candidate : throw new FileNotFoundException("找不到原生固件工具，请安装完整 BlueHead 包。");
    }
    private static async Task<JsonElement> RunFirmwareTool(IEnumerable<string> arguments, CancellationToken cancellation)
    {
        var start = new ProcessStartInfo(RuntimeTool()) { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        foreach (string argument in arguments) start.AddArgument(argument);
        using var process = Process.Start(start) ?? throw new IOException("无法启动固件工具。");
        ProcessTree owned;
        try { owned = new ProcessTree(process); }
        catch { if (!process.HasExited) process.Kill(); throw; }
        using var tree = owned;
        Task<string> stdout = process.StandardOutput.ReadToEndAsync(), stderr = process.StandardError.ReadToEndAsync();
        Task exit = process.WaitForExitAsync();
        if (await Task.WhenAny(exit, Task.Delay(TimeSpan.FromMinutes(3), cancellation)) != exit)
        {
            tree.Terminate(); await exit.WaitAsync(TimeSpan.FromSeconds(5));
            cancellation.ThrowIfCancellationRequested();
            throw new IOException("固件准备超时。可重试，或从原厂包导入。");
        }
        await Task.WhenAll(stdout, stderr);
        if (process.ExitCode != 0)
        {
            string detail = stderr.Result;
            try { using var failure = JsonDocument.Parse(detail); detail = failure.RootElement.Text("error"); } catch (JsonException) { }
            throw new IOException("固件准备未完成：" + detail);
        }
        using var result = JsonDocument.Parse(stdout.Result);
        return result.RootElement.Clone();
    }
    private async Task PrepareFirmware(string? packagePath)
    {
        if (!AcquireFirmwareButton.IsEnabled || controllerProfile == null || offline) return;
        string profile = controllerProfile;
        driverBusy = true; firmwareCancellation = new(); SetRunning(false);
        DriverProgress.Visibility = Visibility.Visible;
        DriverResult.Text = packagePath == null ? "正在获取并验证匹配固件…" : "正在静态提取并验证固件，不会运行原厂安装程序…";
        try
        {
            var arguments = new List<string> { "--firmware", "ensure", "--firmware-profile", profile };
            using var lifetime = new ExtractionLifetime();
            if (packagePath != null)
            {
                JsonElement catalog = await RunFirmwareTool(["--firmware", "inspect"], firmwareCancellation.Token);
                JsonElement selected = catalog.GetProperty("profiles").EnumerateArray().Single(item => item.Text("id") == profile);
                var names = new[] { "image", "config", "license" }.Select(role =>
                    Path.GetFileName(catalog.GetProperty("artifacts").GetProperty(selected.Text(role)).Text("path")));
                lifetime.Package = new FirmwarePackage(Path.Combine(dataDirectory, "firmware-import"), names);
                await Task.Run(() => lifetime.Package.Extract(packagePath));
                firmwareCancellation.Token.ThrowIfCancellationRequested();
                arguments.Add("--firmware-import"); arguments.Add(lifetime.Package.DirectoryPath);
            }
            // License files may be absent from an OEM package; only signed
            // source entries can supply the remainder, verified in native code.
            arguments.Add("--allow-network");
            await RunFirmwareTool(arguments, firmwareCancellation.Token);
            ClearControllerFailure(); DriverResult.Text = "启动文件已准备好。回到“音频中心”，点击右上角“开始接收”。";
        }
        catch (OperationCanceledException) { DriverResult.Text = "已取消。原有配对和驱动未改变。"; return; }
        catch (Exception error) { DriverResult.Text = error.Message; return; }
        finally
        {
            firmwareCancellation.Dispose(); firmwareCancellation = null; driverBusy = false;
            DriverProgress.Visibility = Visibility.Collapsed; SetRunning(false);
        }
        UpdateDriverActions(); UpdateSetupState();
    }
    private sealed class ExtractionLifetime : IDisposable { public FirmwarePackage? Package; public void Dispose() => Package?.Dispose(); }
    private async void AcquireFirmwareClick(object sender, RoutedEventArgs e) => await PrepareFirmware(null);
    private async void ImportFirmwareClick(object sender, RoutedEventArgs e)
    {
        if (!ImportFirmwareButton.IsEnabled) return;
        var picker = new OpenFileDialog { Title = "选择原厂蓝牙驱动包（只提取，不执行）", Filter = "原厂驱动包|*.zip;*.cab;*.msi;*.exe|所有文件|*.*" };
        if (picker.ShowDialog(this) == true) await PrepareFirmware(picker.FileName);
    }
    private void CancelFirmwareClick(object sender, RoutedEventArgs e) => firmwareCancellation?.Cancel();
    private async void OriginalBootstrapClick(object sender, RoutedEventArgs e) => await DriverOperation("initialize-original");
    private void RetryControllerClick(object sender, RoutedEventArgs e)
    {
        if (!RetryControllerButton.IsEnabled) return;
        ClearControllerFailure(); DriverResult.Text = "回到“音频中心”，点击右上角“开始接收”重试。";
    }
}
