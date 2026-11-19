namespace BlueHead.Platform;

/// <summary>A dead helper must not leave the next operation permanently blocked.
/// Thread-owned: callers must acquire and dispose on the same synchronous worker,
/// never across an await continuation.</summary>
internal sealed class DriverOperationLease : IDisposable
{
    private readonly Mutex mutex = new(false, @"Global\BlueHead.DriverOperation");
    private bool acquired;
    public DriverOperationLease()
    {
        try
        {
            try { acquired = mutex.WaitOne(0); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired) throw new IOException("另一个蓝牙驱动操作正在执行，请等待完成后重新检测。");
        }
        catch { mutex.Dispose(); throw; }
    }
    public void Dispose()
    {
        if (acquired) { acquired = false; mutex.ReleaseMutex(); }
        mutex.Dispose();
    }
}
