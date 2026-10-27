namespace BlueHead.Platform;

public enum DriverHandoffOutcome { DedicatedReady, SystemRestored, RestartWindows }
public interface IDriverHandoff
{
    bool RestartRequired { get; }
    bool SystemReady { get; }
    void Stage(string name);
    void RestoreOriginal();
    void BindDedicated();
    bool ProbeDedicated();
}
/// <summary>One explicit handoff; no loops, hidden retries or unverified ready state.</summary>
public static class DriverHandoff
{
    public static DriverHandoffOutcome Run(IDriverHandoff port)
    {
        port.Stage("bootstrap_restoring_original"); port.RestoreOriginal();
        if (port.RestartRequired) return DriverHandoffOutcome.RestartWindows;
        if (!port.SystemReady) throw new IOException("原厂驱动尚未完成蓝牙初始化。");
        port.Stage("bootstrap_original_ready"); port.BindDedicated();
        if (!port.RestartRequired && port.ProbeDedicated()) return DriverHandoffOutcome.DedicatedReady;
        port.Stage("bootstrap_returning_to_windows"); port.RestoreOriginal();
        return port.RestartRequired ? DriverHandoffOutcome.RestartWindows : DriverHandoffOutcome.SystemRestored;
    }
}
