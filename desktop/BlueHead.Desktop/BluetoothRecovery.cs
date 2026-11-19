using BlueHead.Platform;
using System.IO;

namespace BlueHead.Desktop;

public partial class MainWindow
{
    private CancellationTokenSource? automaticDriverRecheck;
    private void ScheduleDriverRecheck()
    {
        if (offline || closing || automaticDriverRecheck != null) return;
        var cancellation = automaticDriverRecheck = new();
        _ = Observe();
        async Task Observe()
        {
            try
            {
                foreach (var delay in new[] { 500, 2000, 5000 })
                {
                    await Task.Delay(delay, cancellation.Token);
                    if (closing || driverBusy) return;
                    await RefreshDrivers();
                    if (!usageOutcomeFailed && CurrentControllerGuide == null &&
                        driverInventory.All(device => !device.RebootRequired)) return;
                }
            }
            catch (OperationCanceledException) { }
            finally
            {
                if (ReferenceEquals(automaticDriverRecheck, cancellation)) automaticDriverRecheck = null;
                cancellation.Dispose();
            }
        }
    }
    private void CancelDriverRecheck()
    {
        automaticDriverRecheck?.Cancel();
        automaticDriverRecheck = null;
    }
    private static string DriverSnapshotKey(List<DriverDevice> devices, bool ready) =>
        ready + "|" + string.Join("|", devices.OrderBy(item => item.InstanceId).Select(item =>
            $"{item.InstanceId}:{item.Service}:{item.Healthy}:{item.RebootRequired}:{item.SystemRadioReady}"));

    private async Task<(List<DriverDevice> Devices, bool SystemReady)> ConfirmDriverInventory()
    {
        string? previous = null;
        int confirmations = 0;
        for (int sample = 0; sample < 9 && !closing; sample++)
        {
            var snapshot = await Task.Run(() => (Devices: Drivers.Enumerate(), SystemReady: Drivers.SystemRadioAvailable()));
            string key = DriverSnapshotKey(snapshot.Devices, snapshot.SystemReady);
            confirmations = key == previous ? confirmations + 1 : 1;
            previous = key;
            if (confirmations >= 3) return snapshot;
            await Task.Delay(300);
        }
        throw new IOException("蓝牙状态仍在变化，请稍候再点击“重新检测”。");
    }

    private void ReconcileConfirmedDriverState(List<DriverDevice> inventory)
    {
        var restored = inventory.FirstOrDefault(item => item.InstanceId == driverResultInstance &&
            item.Healthy && !item.RebootRequired && (BluetoothSetup.Dedicated(item) ? controllerCode == null : item.SystemRadioReady == true));
        if (restored == null) return;
        var previous = DriverResult.Text;
        DriverResult.Text = BluetoothSetup.Dedicated(restored) ? "专用驱动已就绪。" : "Windows 蓝牙已恢复，可以连接设备。";
        DriverResult.Foreground = UsageBrush("#078B4E");
        driverDiagnostic = null; usageOutcomeTitle = usageOutcomeDetail = null;
        usageOutcomeFailed = false;
        if (NoticeText.Text == previous) Notice.Visibility = System.Windows.Visibility.Collapsed;
    }
}
