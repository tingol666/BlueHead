using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace BlueHead.Desktop;

// Work areas are physical pixels. Layout remains in WPF device-independent
// units, and the final placement uses physical coordinates for mixed-DPI screens.
internal static class WindowSizing
{
    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    private struct MonitorInfo { public int Size; public NativeRect Monitor, Work; public uint Flags; }
    [DllImport("user32.dll")] private static extern nint MonitorFromWindow(nint window, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool GetMonitorInfo(nint monitor, ref MonitorInfo info);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(nint window);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint window, out NativeRect rect);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(nint window, nint after, int x, int y, int width, int height, uint flags);

    internal static Size Preferred(Size work)
    {
        var availableWidth = Math.Max(1, work.Width - Math.Min(48, work.Width * .08));
        var availableHeight = Math.Max(1, work.Height - Math.Min(48, work.Height * .08));
        return new(Math.Min(availableWidth, Math.Clamp(work.Width * .58, 860, 980)),
                   Math.Min(availableHeight, Math.Clamp(work.Height * .67, 500, 620)));
    }
    internal static void Track(Window window)
    {
        window.SourceInitialized += (_, _) =>
        {
            if (PresentationSource.FromVisual(window) is HwndSource source)
                source.AddHook((nint hwnd, int message, nint wparam, nint lparam, ref bool handled) =>
                {
                    if (message is 0x02e0 or 0x007e or 0x001a)
                        window.Dispatcher.BeginInvoke(() => Apply(window, false));
                    return 0;
                });
        };
        window.Loaded += (_, _) => Apply(window, true);
    }
    internal static void Apply(Window window, bool initial)
    {
        if (window.WindowState != WindowState.Normal) return;
        var handle = new WindowInteropHelper(window).Handle;
        if (handle == 0) return;
        var info = new MonitorInfo { Size = Marshal.SizeOf<MonitorInfo>() };
        if (!GetMonitorInfo(MonitorFromWindow(handle, 2), ref info)) return;
        var dpi = Math.Max(96, GetDpiForWindow(handle)) / 96.0;
        var work = new Size((info.Work.Right - info.Work.Left) / dpi, (info.Work.Bottom - info.Work.Top) / dpi);
        var preferred = Preferred(work);
        window.MinWidth = Math.Min(860, preferred.Width);
        window.MinHeight = Math.Min(480, preferred.Height);
        window.Width = initial ? preferred.Width : Math.Min(window.Width, work.Width - Math.Min(48, work.Width * .08));
        window.Height = initial ? preferred.Height : Math.Min(window.Height, work.Height - Math.Min(48, work.Height * .08));
        var pixelWidth = (int)Math.Round(window.Width * dpi);
        var pixelHeight = (int)Math.Round(window.Height * dpi);
        if (!GetWindowRect(handle, out var current)) return;
        var x = initial ? info.Work.Left + (info.Work.Right - info.Work.Left - pixelWidth) / 2 : Math.Clamp(current.Left, info.Work.Left, Math.Max(info.Work.Left, info.Work.Right - pixelWidth));
        var y = initial ? info.Work.Top + (info.Work.Bottom - info.Work.Top - pixelHeight) / 2 : Math.Clamp(current.Top, info.Work.Top, Math.Max(info.Work.Top, info.Work.Bottom - pixelHeight));
        SetWindowPos(handle, 0, x, y, pixelWidth, pixelHeight, 0x0014); // NOZORDER | NOACTIVATE
    }
    internal static bool FitsCurrentWorkArea(Window window)
    {
        var handle = new WindowInteropHelper(window).Handle;
        var info = new MonitorInfo { Size = Marshal.SizeOf<MonitorInfo>() };
        return GetWindowRect(handle, out var rect) && GetMonitorInfo(MonitorFromWindow(handle, 2), ref info)
            && rect.Left >= info.Work.Left && rect.Top >= info.Work.Top
            && rect.Right <= info.Work.Right && rect.Bottom <= info.Work.Bottom;
    }
}
