using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace BlueHead.Desktop;

public sealed class EngineClient : IDisposable
{
    private Process? process;
    private readonly SemaphoreSlim writer = new(1, 1);
    private readonly CancellationTokenSource lifetime = new();
    public event Action<JsonElement>? Message;
    public event Action<string>? Failed;
    public bool Ready { get; private set; }
    public async Task StartAsync(string dataDirectory)
    {
        var packaged = Path.Combine(AppContext.BaseDirectory, "engine", "BlueHead.Engine.exe");
        var root = Environment.GetEnvironmentVariable("BLUEHEAD_SOURCE_ROOT");
        ProcessStartInfo info;
        if (File.Exists(packaged)) info = new(packaged);
        else if (root != null && File.Exists(Path.Combine(root, "launch.py")))
        {
            info = new(Path.Combine(root, ".venv", "Scripts", "python.exe"));
            info.ArgumentList.Add("-X"); info.ArgumentList.Add("utf8");
            info.ArgumentList.Add(Path.Combine(root, "launch.py"));
        }
        else throw new FileNotFoundException("找不到蓝牙后台，请重新解压完整安装包。");
        info.ArgumentList.Add("--engine"); info.ArgumentList.Add("--data-dir"); info.ArgumentList.Add(dataDirectory);
        info.UseShellExecute = false; info.CreateNoWindow = true;
        info.RedirectStandardInput = info.RedirectStandardOutput = info.RedirectStandardError = true;
        info.StandardOutputEncoding = info.StandardErrorEncoding = Encoding.UTF8;
        info.StandardInputEncoding = new UTF8Encoding(false);
        process = Process.Start(info) ?? throw new IOException("蓝牙后台未能启动。");
        _ = ReadMessagesAsync(process, lifetime.Token);
        _ = DrainErrorsAsync(process, lifetime.Token);
        await Task.CompletedTask;
    }
    private async Task ReadMessagesAsync(Process child, CancellationToken cancellation)
    {
        try
        {
            while (await child.StandardOutput.ReadLineAsync(cancellation) is { } line)
            {
                if (line.Length > 262144) throw new IOException("后台响应过长。");
                using var document = JsonDocument.Parse(line);
                var value = document.RootElement;
                if (!value.TryGetProperty("protocol", out var protocol) || protocol.GetInt32() != 1)
                    throw new IOException("界面与后台版本不匹配。");
                if (value.Text("event") == "hello") { ValidateVersion(value); Ready = true; }
                Message?.Invoke(value.Clone());
            }
            if (!cancellation.IsCancellationRequested) Failed?.Invoke("蓝牙后台已退出。可重新打开程序；驱动恢复工具仍可独立使用。");
        }
        catch (OperationCanceledException) { }
        catch (Exception exception) { if (!cancellation.IsCancellationRequested) Failed?.Invoke(exception.Message); }
        finally { Ready = false; }
    }
    private static async Task DrainErrorsAsync(Process child, CancellationToken cancellation)
    {
        try { while (await child.StandardError.ReadLineAsync(cancellation) != null) { } }
        catch (OperationCanceledException) { }
        catch (IOException) { }
    }
    internal static void ValidateVersion(JsonElement hello)
    {
        if (hello.Text("version") != AppVersion.Number)
            throw new IOException($"界面版本 {AppVersion.Number} 与后台版本 {hello.Text("version", "未知")} 不一致，请完整更新程序。");
    }
    public async Task SendAsync(object message)
    {
        if (process is null || process.HasExited) throw new IOException("蓝牙后台尚未就绪。");
        var json = JsonSerializer.SerializeToElement(message);
        var envelope = new Dictionary<string, object?> { ["protocol"] = 1 };
        foreach (var property in json.EnumerateObject()) envelope[property.Name] = property.Value;
        await writer.WaitAsync();
        try { await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(envelope)); await process.StandardInput.FlushAsync(); }
        finally { writer.Release(); }
    }
    public async Task ShutdownAsync()
    {
        if (process is null || process.HasExited) return;
        try
        {
            await SendAsync(new { command = "shutdown" });
            process.StandardInput.Close();
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));
        }
        catch (TimeoutException) { process.Kill(entireProcessTree: true); }
        catch (IOException) { }
        catch (InvalidOperationException) { }
    }
    public void Dispose() { lifetime.Cancel(); process?.Dispose(); writer.Dispose(); lifetime.Dispose(); }
}
