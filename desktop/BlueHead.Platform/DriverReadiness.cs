namespace BlueHead.Platform;

public enum DriverReadinessAction { Wait, Ready, RestartDevice, RestartWindows, Failed }
public readonly record struct DriverObservation(bool Present, bool Dedicated, bool Healthy, bool RestartRequired, bool RadioReady);

/// <summary>Pure transition policy; driver binding and firmware/radio readiness are different facts.</summary>
public static class DriverReadiness
{
    public static DriverReadinessAction Evaluate(DriverObservation state, bool dedicated, TimeSpan elapsed, bool restarted)
    {
        if (state.Present && !state.RestartRequired && state.Dedicated == dedicated && state.Healthy && (dedicated || state.RadioReady))
            return DriverReadinessAction.Ready;
        // Pending restart is observable state, not a reason to skip the supported
        // device-level restart. A timeout alone cannot prove a PC reboot is needed.
        if (elapsed >= TimeSpan.FromSeconds(45))
            return state.Present && state.RestartRequired ? DriverReadinessAction.RestartWindows : DriverReadinessAction.Failed;
        if (!restarted && state.Present && state.Dedicated == dedicated &&
            elapsed >= TimeSpan.FromSeconds(state.RestartRequired ? 2 : 15))
            return DriverReadinessAction.RestartDevice;
        return DriverReadinessAction.Wait;
    }
    public static bool ValidRadioAddress(ulong address) => address != 0 && address != 0xffffffffffff && address != 0x0003199e8b00 && address <= 0xffffffffffff;
}

/// <summary>Confirm both failure and recovery over independent observations.</summary>
public sealed class DriverReadinessTracker
{
    private int readyCount, failureCount;
    private DriverReadinessAction previous;
    public DriverReadinessAction Observe(DriverObservation state, bool dedicated, TimeSpan elapsed, bool restarted)
    {
        var action = DriverReadiness.Evaluate(state, dedicated, elapsed, restarted);
        if (action == DriverReadinessAction.Ready)
        {
            failureCount = 0; previous = action;
            return ++readyCount >= 3 ? action : DriverReadinessAction.Wait;
        }
        readyCount = 0;
        if (action == DriverReadinessAction.Wait) { failureCount = 0; previous = action; return action; }
        failureCount = previous == action ? failureCount + 1 : 1;
        previous = action;
        return failureCount >= 3 ? action : DriverReadinessAction.Wait;
    }
}
