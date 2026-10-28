using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using BlueHead.Platform;

namespace BlueHead.Desktop;

// A replacement cannot start until the previous kill-on-close job is empty,
// including any child left behind by a failed native host.
public sealed class EngineClient : IDisposable
{
    internal sealed class Limits
    {
        public TimeSpan Hello = TimeSpan.FromSeconds(20), Pulse = TimeSpan.FromSeconds(2),
            Unresponsive = TimeSpan.FromSeconds(15), Stop = TimeSpan.FromSeconds(25),
            Write = TimeSpan.FromSeconds(2);
    }
    private sealed class Session(int generation, Process process, ProcessTree tree, string directory)
    {
        public readonly int Generation = generation;
        public readonly Process Process = process;
        public readonly ProcessTree Tree = tree;
        public readonly string Directory = directory;
        public readonly StreamWriter Input = new(process.StandardInput.BaseStream, new UTF8Encoding(false));
        public readonly CancellationTokenSource Cancel = new();
        public readonly TaskCompletionSource<JsonElement> Hello = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public Task Reader = Task.CompletedTask, Errors = Task.CompletedTask, Monitor = Task.CompletedTask;
        public Task PendingWrite = Task.CompletedTask;
        public volatile bool Ready, Failed, ExpectedExit;
        public long LastPulse = Stopwatch.GetTimestamp(), StopDeadline;
        public readonly object State = new();
        public string ErrorTail = "", Failure = "";
    }
    private readonly SemaphoreSlim lifecycle = new(1, 1), writer = new(1, 1);
    private readonly Func<string, ProcessStartInfo> createStart;
    private readonly Limits limits;
    private volatile Session? current;
    private int generation;
    private bool disposed;
    public event Action<int, JsonElement>? Message;
    public event Action<int, string>? Failed;
    public bool Ready
    {
        get
        {
            var session = current;
            if (session is not { Ready: true, Failed: false }) return false;
            try { return !session.Process.HasExited; }
            catch (InvalidOperationException) { return false; }
        }
    }
    public int Generation => current?.Generation ?? generation;
    internal JsonElement? Hello => current is { Ready: true } session && session.Hello.Task.Status == TaskStatus.RanToCompletion ? session.Hello.Task.Result : null;
    public EngineClient() : this(CreateStart, new Limits()) { }
    internal EngineClient(Func<string, ProcessStartInfo> createStart, Limits limits)
    { this.createStart = createStart; this.limits = limits; }

    private static ProcessStartInfo CreateStart(string dataDirectory)
    {
        var packaged = Path.Combine(AppContext.BaseDirectory, "engine", "BlueHead.Runtime.exe");
        var root = Environment.GetEnvironmentVariable("BLUEHEAD_SOURCE_ROOT");
        ProcessStartInfo info;
        if (File.Exists(packaged)) info = new(packaged);
        else if (root != null && File.Exists(Path.Combine(root, "launch.py")))
        {
            var runtime = Path.Combine(root, "bluehead", "assets", "BlueHead.Runtime.exe");
            if (!File.Exists(runtime)) throw new FileNotFoundException($"请先构建 {AppVersion.Number} 原生音频后台。");
            info = new(runtime);
            info.AddArgument("--source-root"); info.AddArgument(root);
        }
        else throw new FileNotFoundException("找不到蓝牙后台，请重新解压完整安装包。");
        info.AddArgument("--engine"); info.AddArgument("--data-dir"); info.AddArgument(dataDirectory);
        return info;
    }
    public async Task StartAsync(string dataDirectory)
    {
        await lifecycle.WaitAsync().ConfigureAwait(false);
        try
        {
            if (disposed) throw new ObjectDisposedException(nameof(EngineClient));
            if (Ready) return;
            await RetireAsync().ConfigureAwait(false);
            var info = createStart(dataDirectory);
            info.UseShellExecute = false; info.CreateNoWindow = true;
            info.RedirectStandardInput = info.RedirectStandardOutput = info.RedirectStandardError = true;
            info.StandardOutputEncoding = info.StandardErrorEncoding = Encoding.UTF8;
            var process = Process.Start(info) ?? throw new IOException("蓝牙后台未能启动。");
            ProcessTree tree;
            try { tree = new ProcessTree(process); }
            catch { if (!process.HasExited) process.Kill(); process.Dispose(); throw; }
            var session = new Session(++generation, process, tree, dataDirectory);
            current = session;
            session.Reader = ReadMessagesAsync(session);
            session.Errors = DrainErrorsAsync(session);
            session.Monitor = MonitorAsync(session);
            try
            {
                await session.Hello.Task.WaitAsync(limits.Hello).ConfigureAwait(false);
                if (!Ready) throw new IOException(session.Failure);
            }
            catch (Exception error)
            {
                Fail(session, error is TimeoutException ? "蓝牙后台启动超时，请点击开始接收重试。" : error.Message);
                await RetireAsync().ConfigureAwait(false);
                throw new IOException(session.Failure, error);
            }
        }
        finally { lifecycle.Release(); }
    }
    private async Task ReadMessagesAsync(Session session)
    {
        try
        {
            while (!session.Cancel.IsCancellationRequested && await session.Process.StandardOutput.ReadLineAsync().ConfigureAwait(false) is { } line)
            {
                if (line.Length > 262144) throw new IOException("后台响应过长。");
                using var document = JsonDocument.Parse(line);
                var value = document.RootElement;
                ValidateEnvelope(value);
                if (session != current || session.Failed) continue;
                var kind = value.Text("event");
                if (kind == "hello")
                {
                    ValidateVersion(value);
                    var hello = value.Clone();
                    session.Ready = true;
                    Interlocked.Exchange(ref session.LastPulse, Stopwatch.GetTimestamp());
                    Message?.Invoke(session.Generation, hello);
                    session.Hello.TrySetResult(hello);
                    continue;
                }
                if (kind == "heartbeat") { Interlocked.Exchange(ref session.LastPulse, Stopwatch.GetTimestamp()); continue; }
                if (kind == "stopped")
                {
                    Interlocked.Exchange(ref session.StopDeadline, 0);
                    Interlocked.Exchange(ref session.LastPulse, Stopwatch.GetTimestamp());
                }
                Message?.Invoke(session.Generation, value.Clone());
            }
            if (!session.ExpectedExit && !session.Cancel.IsCancellationRequested)
            {
                try { await session.Process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(1)).ConfigureAwait(false); }
                catch (TimeoutException) { }
                Fail(session, "蓝牙后台已中断，连接已清除。点击开始接收可重新连接设备。");
            }
        }
        catch (Exception error) { if (!session.Cancel.IsCancellationRequested) Fail(session, error.Message); }
        finally { session.Ready = false; }
    }
    private static async Task DrainErrorsAsync(Session session)
    {
        try
        {
            var buffer = new char[1024];
            int count;
            while ((count = await session.Process.StandardError.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                lock (session.State)
                {
                    session.ErrorTail += new string(buffer, 0, count);
                    if (session.ErrorTail.Length > 8192) session.ErrorTail = session.ErrorTail.Substring(session.ErrorTail.Length - 8192);
                }
        }
        catch (Exception error) when (error is IOException or ObjectDisposedException or InvalidOperationException) { }
    }
    private async Task MonitorAsync(Session session)
    {
        try
        {
            while (!session.Cancel.IsCancellationRequested && !session.Failed)
            {
                await Task.Delay(limits.Pulse, session.Cancel.Token).ConfigureAwait(false);
                if (session.ExpectedExit) continue;
                // A descendant can keep stdout open after the host has died.
                // Process lifetime, pipe lifetime and heartbeat are independent.
                if (session.Process.HasExited)
                { Fail(session, "蓝牙后台已中断，连接已清除。点击开始接收可重新连接设备。"); continue; }
                if (!session.Ready) continue;
                var now = Stopwatch.GetTimestamp();
                var deadline = Interlocked.Read(ref session.StopDeadline);
                if (deadline != 0)
                {
                    if (now >= deadline) Fail(session, "接收停止超时，旧后台已终止。点击开始接收可重新连接设备。");
                    continue;
                }
                if ((double)(now - Interlocked.Read(ref session.LastPulse)) / Stopwatch.Frequency > limits.Unresponsive.TotalSeconds)
                { Fail(session, "蓝牙后台长时间未响应，连接已清除。点击开始接收可恢复。"); continue; }
                await WriteAsync(session, new { command = "heartbeat" }).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) { }
        catch (IOException) { }
    }
    private void Fail(Session session, string reason)
    {
        lock (session.State)
        {
            if (session.Failed || session.ExpectedExit || session.Cancel.IsCancellationRequested) return;
            session.Failed = true; session.Ready = false; session.Failure = reason;
        }
        session.Hello.TrySetException(new IOException(reason));
        int? code = null;
        try { if (session.Process.HasExited) code = session.Process.ExitCode; } catch (InvalidOperationException) { }
        try { session.Tree.Terminate(); } catch (System.ComponentModel.Win32Exception) { }
        RecordFailure(session, code);
        if (session == current) Failed?.Invoke(session.Generation, reason);
    }
    private static void RecordFailure(Session session, int? code)
    {
        try
        {
            // Local bounded diagnostics only; stderr never becomes a UI notice.
            Directory.CreateDirectory(session.Directory);
            var path = Path.Combine(session.Directory, "engine-supervisor.log");
            if (File.Exists(path) && new FileInfo(path).Length > 131072)
            { var old = path + ".1"; if (File.Exists(old)) File.Delete(old); File.Move(path, old); }
            lock (session.State)
                File.AppendAllText(path, JsonSerializer.Serialize(new { time_utc = DateTime.UtcNow, session = session.Generation,
                    reason = session.Failure, exit_code = code, stderr_tail = session.ErrorTail }) + Environment.NewLine, Encoding.UTF8);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
    }
    internal static void ValidateEnvelope(JsonElement message)
    {
        if (message.ValueKind != JsonValueKind.Object || !message.TryGetProperty("protocol", out var protocol) ||
            protocol.ValueKind != JsonValueKind.Number || !protocol.TryGetInt32(out var version) || version != 1)
            throw new IOException("界面与后台版本不匹配。");
    }
    internal static void ValidateVersion(JsonElement hello)
    {
        if (hello.Text("version") != AppVersion.Number)
            throw new IOException($"界面版本 {AppVersion.Number} 与后台版本 {hello.Text("version", "未知")} 不一致，请完整更新程序。");
    }
    public Task SendAsync(object message)
    {
        var session = current;
        if (session is null || !Ready) throw new IOException("蓝牙后台未连接，请点击开始接收恢复。");
        return WriteAsync(session, message);
    }
    private async Task WriteAsync(Session session, object message)
    {
        var json = JsonSerializer.SerializeToElement(message);
        var envelope = new Dictionary<string, object?> { ["protocol"] = 1 };
        foreach (var property in json.EnumerateObject()) envelope[property.Name] = property.Value;
        var command = json.Text("command");
        if (command is "stop" or "shutdown")
            Interlocked.CompareExchange(ref session.StopDeadline, Stopwatch.GetTimestamp() + (long)(limits.Stop.TotalSeconds * Stopwatch.Frequency), 0);
        var locked = false;
        try
        {
            locked = await writer.WaitAsync(limits.Write).ConfigureAwait(false);
            if (!locked) throw new TimeoutException();
            if (session != current || session.Failed) throw new IOException("蓝牙后台会话已失效，请重新开始接收。");
            session.PendingWrite = session.Input.WriteLineAsync(JsonSerializer.Serialize(envelope));
            await session.PendingWrite.WaitAsync(limits.Write).ConfigureAwait(false);
            session.PendingWrite = session.Input.FlushAsync();
            await session.PendingWrite.WaitAsync(limits.Write).ConfigureAwait(false);
        }
        catch (Exception error) when (error is IOException or InvalidOperationException or ObjectDisposedException or TimeoutException)
        {
            Fail(session, "蓝牙后台通信中断，连接已清除。点击开始接收可恢复。");
            throw new IOException(session.Failure.Length == 0 ? "蓝牙后台正在退出。" : session.Failure, error);
        }
        finally { if (locked) writer.Release(); }
    }
    private async Task RetireAsync()
    {
        var session = current;
        if (session is null) return;
        session.Ready = false; session.Cancel.Cancel();
        session.Tree.Terminate();
        await session.Tree.WaitForEmptyAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
        await Task.WhenAll(session.Reader, session.Errors, session.Monitor).WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
        await writer.WaitAsync().ConfigureAwait(false);
        try
        {
            // A timed-out pipe write is still an OS operation until killing the
            // owned reader retires it. Never dispose its StreamWriter early.
            try { await session.PendingWrite.WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false); }
            catch (Exception error) when (error is IOException or InvalidOperationException or ObjectDisposedException) { }
            try { session.Input.Dispose(); }
            catch (IOException) { } // The verified-dead child's pipe may be broken.
            finally { session.Process.Dispose(); session.Tree.Dispose(); session.Cancel.Dispose(); current = null; }
        }
        finally { writer.Release(); }
    }
    public async Task ShutdownAsync()
    {
        await lifecycle.WaitAsync().ConfigureAwait(false);
        try
        {
            var session = current;
            if (session is null) return;
            session.ExpectedExit = true;
            if (!session.Process.HasExited && !session.Failed)
            {
                try
                {
                    await WriteAsync(session, new { command = "shutdown" }).ConfigureAwait(false);
                    session.Input.Close();
                    await session.Process.WaitForExitAsync().WaitAsync(limits.Stop).ConfigureAwait(false);
                }
                catch (Exception error) when (error is TimeoutException or IOException or InvalidOperationException) { }
            }
            await RetireAsync().ConfigureAwait(false);
        }
        finally { lifecycle.Release(); }
    }
    public void Dispose()
    {
        lifecycle.Wait();
        try { if (disposed) return; disposed = true; RetireAsync().GetAwaiter().GetResult(); }
        finally { lifecycle.Release(); }
    }
}
