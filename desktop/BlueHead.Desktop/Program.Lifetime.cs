using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using Application = System.Windows.Application;

namespace BlueHead.Desktop;

internal static partial class Program
{
    private static int ExitSelfTest(Application app, string report)
    {
        app.ShutdownMode = ShutdownMode.OnExplicitShutdown;
        var checks = new Dictionary<string, bool>();
        var unhandled = new List<string>();
        app.DispatcherUnhandledException += (_, error) =>
        { unhandled.Add(error.Exception.GetType().Name); error.Handled = true; };
        try
        {
            var baseline = new Window { Style = null, Title = "BlueHead exit regression", Width = 1, Height = 1, ShowInTaskbar = false, Left = -10000, Top = -10000 };
            var reentrantRejected = false;
            CancelEventHandler oldPattern = (_, args) =>
            {
                args.Cancel = true;
                try { baseline.Close(); }
                catch (InvalidOperationException) { reentrantRejected = true; }
            };
            baseline.Closing += oldPattern;
            baseline.Show(); baseline.Close();
            checks["old_close_inside_closing_pattern_reproduces_wpf_failure"] = reentrantRejected;
            baseline.Closing -= oldPattern; baseline.Close();

            void Case(string name, Func<Task> cleanup, bool delayed = false, Action? release = null, int errors = 0)
            {
                var calls = 0; var closed = false;
                var window = new MainWindow(true)
                {
                    SelfTestExitLifecycle = true,
                    SelfTestShutdown = () => { calls++; return cleanup(); }
                };
                app.MainWindow = window;
                window.Closed += (_, _) => closed = true;
                window.Show(); Pump(20); window.Hide();
                window.Close();
                checks[name + "_final_close_is_deferred"] = !closed && window.ExitTask != null;
                if (delayed)
                {
                    Pump(40); window.Close(); Pump(20);
                    checks[name + "_repeated_exit_does_not_start_duplicate_cleanup"] = calls == 1 && !closed;
                    release!();
                }
                for (var attempt = 0; attempt < 100 && window.ExitTask?.IsCompleted != true; attempt++) Pump(20);
                checks[name + "_closes_and_releases_hidden_window"] = closed && calls == 1 && window.ExitTask?.Status == TaskStatus.RanToCompletion;
                checks[name + "_cleanup_errors_cannot_strand_window"] = window.SelfTestExitErrors.Count == errors;
            }
            Case("already_exited_backend", () => Task.CompletedTask);
            Case("cleanup_failure", () => Task.FromException(new IOException("Synthetic cleanup failure")), errors: 1);
            var pending = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            Case("pending_cleanup", () => pending.Task, delayed: true, release: () => pending.SetResult(true));
            checks["no_unhandled_exit_exception_or_modal_error"] = unhandled.Count == 0;
        }
        catch (Exception error)
        { checks["test_completed"] = false; unhandled.Add(error.ToString()); }
        report = Path.GetFullPath(report);
        Directory.CreateDirectory(Path.GetDirectoryName(report)!);
        var passed = checks.Values.All(value => value);
        File.WriteAllText(report, JsonSerializer.Serialize(new { passed, checks, errors = unhandled,
            scope = "Actual WPF Window Closing and MainWindow retirement with absent backend; no audio, USB, user settings or driver changes" }, new JsonSerializerOptions { WriteIndented = true }));
        app.Shutdown();
        return passed ? 0 : 1;
    }
}
