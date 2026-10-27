using BlueHead.Platform;

internal static class HandoffChecks
{
    private sealed class Port(string fault) : IDriverHandoff
    {
        public string Owner = "dedicated";
        public int Restores, Binds, Probes;
        public bool RestartRequired { get; private set; }
        public bool SystemReady => Owner == "windows" && fault != "not-ready";
        public void Stage(string name) { }
        public void RestoreOriginal() { Restores++; Owner = "windows"; RestartRequired = fault == "restart-original"; }
        public void BindDedicated()
        {
            if (Owner != "windows") throw new IOException("overlapping driver owners");
            Binds++; Owner = "dedicated"; RestartRequired = fault == "restart-dedicated";
        }
        public bool ProbeDedicated()
        {
            if (Owner != "dedicated") throw new IOException("native USB opened under original driver");
            Probes++; return fault != "firmware-lost";
        }
    }
    internal static void Run(IDictionary<string, bool> checks)
    {
        var good = new Port("none"); var passed = DriverHandoff.Run(good);
        checks["handoff_probes_after_owner_transfer"] = passed == DriverHandoffOutcome.DedicatedReady && good.Restores == 1 && good.Binds == 1 && good.Probes == 1 && good.Owner == "dedicated";
        var lost = new Port("firmware-lost"); var fallback = DriverHandoff.Run(lost);
        checks["handoff_lost_firmware_returns_windows_without_loop"] = fallback == DriverHandoffOutcome.SystemRestored && lost.Restores == 2 && lost.Binds == 1 && lost.Probes == 1 && lost.Owner == "windows";
        var originalRestart = new Port("restart-original"); var restart = DriverHandoff.Run(originalRestart);
        checks["handoff_original_restart_stops_before_dedicated"] = restart == DriverHandoffOutcome.RestartWindows && originalRestart.Binds == 0 && originalRestart.Probes == 0;
        var dedicatedRestart = new Port("restart-dedicated"); var rollback = DriverHandoff.Run(dedicatedRestart);
        checks["handoff_rebind_restart_returns_original_without_probe"] = rollback == DriverHandoffOutcome.SystemRestored && dedicatedRestart.Probes == 0 && dedicatedRestart.Owner == "windows";
        var notReady = new Port("not-ready");
        try { DriverHandoff.Run(notReady); checks["handoff_never_accepts_bound_driver_as_firmware_ready"] = false; }
        catch (IOException) { checks["handoff_never_accepts_bound_driver_as_firmware_ready"] = notReady.Binds == 0 && notReady.Probes == 0; }
    }
}
