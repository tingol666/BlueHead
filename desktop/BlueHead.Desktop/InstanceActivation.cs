namespace BlueHead.Desktop;

// Per-user activation only. It carries no settings, audio or process ownership.
internal sealed class InstanceActivation : IDisposable
{
    private readonly EventWaitHandle signal;
    private readonly RegisteredWaitHandle registration;
    private volatile bool disposed;
    internal static string Name => "Local\\BlueHead.Activate." + Environment.UserName;
    internal InstanceActivation(string name, Action activate)
    {
        signal = new EventWaitHandle(false, EventResetMode.AutoReset, name);
        registration = ThreadPool.RegisterWaitForSingleObject(signal, (_, timedOut) =>
        { if (!timedOut && !disposed) activate(); }, null, Timeout.Infinite, false);
    }
    internal static bool TrySignal(string name)
    {
        for (var attempt = 0; attempt < 6; attempt++)
        {
            try { using var existing = EventWaitHandle.OpenExisting(name); return existing.Set(); }
            catch (WaitHandleCannotBeOpenedException) { if (attempt < 5) Thread.Sleep(30); }
            catch (UnauthorizedAccessException) { return false; }
        }
        return false;
    }
    public void Dispose()
    { disposed = true; registration.Unregister(null); signal.Dispose(); }
}
