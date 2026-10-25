using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace System.Runtime.CompilerServices
{
    // C# init/record metadata; no runtime emulation or reflection interception.
    public sealed class IsExternalInit { }
}

namespace BlueHead.Platform
{
    /// <summary>Small, explicit replacements for APIs absent from the Windows inbox CLR.</summary>
    public static class FrameworkCompat
    {
        public static string ProcessPath
        {
            get
            {
                using var process = Process.GetCurrentProcess();
                return process.MainModule?.FileName ?? throw new IOException("无法定位当前程序。");
            }
        }
        public static double Clamp(double value, double minimum, double maximum) => Math.Max(minimum, Math.Min(maximum, value));
        public static int Clamp(int value, int minimum, int maximum) => Math.Max(minimum, Math.Min(maximum, value));
        public static bool IsFinite(double value) => !double.IsNaN(value) && !double.IsInfinity(value);
        public static bool Contains(this string text, string value, StringComparison comparison) => text.IndexOf(value, comparison) >= 0;
        public static TValue? GetValueOrDefault<TKey, TValue>(this IDictionary<TKey, TValue> values, TKey key)
            => values.TryGetValue(key, out var value) ? value : default;

        public static string HashPrefix(string value)
        {
            using var hash = SHA256.Create();
            return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").Substring(0, 24);
        }
        public static string? PtrToStringUtf8(IntPtr pointer)
        {
            if (pointer == IntPtr.Zero) return null;
            var length = 0;
            while (length < 65536 && Marshal.ReadByte(pointer, length) != 0) length++;
            if (length == 65536) throw new IOException("驱动工具返回的字符串过长。");
            var bytes = new byte[length]; Marshal.Copy(pointer, bytes, 0, length);
            return Encoding.UTF8.GetString(bytes);
        }

        // CreateProcess/CommandLineToArgvW quoting: preserve empty strings, quotes,
        // and every trailing backslash, including paths immediately before a quote.
        public static string QuoteArgument(string argument)
        {
            if (argument.IndexOf('\0') >= 0) throw new ArgumentException("命令参数不能包含空字符。", nameof(argument));
            var output = new StringBuilder("\"");
            var backslashes = 0;
            foreach (var character in argument)
            {
                if (character == '\\') { backslashes++; continue; }
                if (character == '"') output.Append('\\', backslashes * 2 + 1).Append('"');
                else output.Append('\\', backslashes).Append(character);
                backslashes = 0;
            }
            return output.Append('\\', backslashes * 2).Append('"').ToString();
        }
        public static void AddArgument(this ProcessStartInfo start, string argument)
        {
            if (start.Arguments.Length > 0) start.Arguments += " ";
            start.Arguments += QuoteArgument(argument);
        }

        public static Task WaitForExitAsync(this Process process)
        {
            if (process.HasExited) return Task.CompletedTask;
            var completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            EventHandler? exited = null;
            exited = (_, _) =>
            {
                process.Exited -= exited;
                completion.TrySetResult(true);
            };
            process.Exited += exited;
            try
            {
                process.EnableRaisingEvents = true;
                if (process.HasExited) exited(process, EventArgs.Empty);
            }
            catch (InvalidOperationException) when (process.HasExited)
            {
                process.Exited -= exited;
                completion.TrySetResult(true);
            }
            catch { process.Exited -= exited; throw; }
            return completion.Task;
        }
        public static async Task WaitAsync(this Task task, TimeSpan timeout)
        {
            using var cancellation = new CancellationTokenSource();
            var timer = Task.Delay(timeout, cancellation.Token);
            if (await Task.WhenAny(task, timer).ConfigureAwait(false) != task) throw new TimeoutException();
            cancellation.Cancel();
            await task.ConfigureAwait(false);
        }
        public static async Task<T> WaitAsync<T>(this Task<T> task, TimeSpan timeout)
        {
            await ((Task)task).WaitAsync(timeout).ConfigureAwait(false);
            return await task.ConfigureAwait(false);
        }

        public static void ReplaceFile(string temporaryPath, string destinationPath)
        {
            if (File.Exists(destinationPath)) File.Replace(temporaryPath, destinationPath, null);
            else File.Move(temporaryPath, destinationPath);
        }
        public static string RelativePath(string directory, string path)
        {
            var root = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var target = Path.GetFullPath(path);
            if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new IOException("备份文件不在恢复目录中。");
            return target.Substring(root.Length);
        }
    }

    /// <summary>Owns the background process tree; closing the job cannot kill unrelated processes.</summary>
    public sealed class ProcessTree : IDisposable
    {
        private IntPtr handle;
        [StructLayout(LayoutKind.Sequential)] private struct BasicLimits
        { public long ProcessTime, JobTime; public uint Flags; public UIntPtr Minimum, Maximum; public uint Active; public UIntPtr Affinity; public uint Priority, Scheduling; }
        [StructLayout(LayoutKind.Sequential)] private struct IoCounters
        { public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits
        { public BasicLimits Basic; public IoCounters Io; public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr CreateJobObject(IntPtr attributes, string? name);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(IntPtr job, int information, ref ExtendedLimits limits, uint length);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        public ProcessTree(Process process)
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                var limits = new ExtendedLimits(); limits.Basic.Flags = 0x2000; // KILL_ON_JOB_CLOSE
                if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf<ExtendedLimits>()) || !AssignProcessToJobObject(handle, process.Handle))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "无法管理蓝牙后台进程。");
            }
            catch { Dispose(); throw; }
        }
        public void Terminate() { if (handle != IntPtr.Zero && !TerminateJobObject(handle, 1)) throw new Win32Exception(Marshal.GetLastWin32Error()); }
        public void Dispose() { var previous = Interlocked.Exchange(ref handle, IntPtr.Zero); if (previous != IntPtr.Zero) CloseHandle(previous); }
    }
}
