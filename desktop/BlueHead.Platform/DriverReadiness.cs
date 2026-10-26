namespace BlueHead.Platform;

public enum DriverReadinessAction { Wait, Ready, RestartDevice, RestartWindows, Failed }
public readonly record struct DriverObservation(bool Present, bool Dedicated, bool Healthy, bool RestartRequired, bool RadioReady);

/// <summary>Pure transition policy; driver binding and firmware/radio readiness are different facts.</summary>
public static class DriverReadiness
{
    public static DriverReadinessAction Evaluate(DriverObservation state, bool dedicated, TimeSpan elapsed, bool restarted)
    {
        if (state.Present && state.RestartRequired) return DriverReadinessAction.RestartWindows;
        if (state.Present && state.Dedicated == dedicated && state.Healthy && (dedicated || state.RadioReady))
            return DriverReadinessAction.Ready;
        if (elapsed >= TimeSpan.FromSeconds(45))
            return state.Present && state.Dedicated == dedicated ? DriverReadinessAction.RestartWindows : DriverReadinessAction.Failed;
        if (!dedicated && !restarted && state.Present && !state.Dedicated && elapsed >= TimeSpan.FromSeconds(15))
            return DriverReadinessAction.RestartDevice;
        return DriverReadinessAction.Wait;
    }
    public static bool ValidRadioAddress(ulong address) => address != 0 && address != 0xffffffffffff && address != 0x0003199e8b00 && address <= 0xffffffffffff;
}
