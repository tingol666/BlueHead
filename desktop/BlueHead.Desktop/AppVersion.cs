using System.Reflection;

namespace BlueHead.Desktop;

public static class AppVersion
{
    public static string Number { get; } = typeof(AppVersion).Assembly
        .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion.Split('+')[0]
        ?? typeof(AppVersion).Assembly.GetName().Version?.ToString(3) ?? "未知版本";
    public static string Label => $"{Number} · 待验收";
    public static string Title => $"BlueHead {Label}";
}
