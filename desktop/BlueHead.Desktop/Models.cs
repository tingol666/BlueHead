using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Globalization;
using System.Windows.Data;
using System.Runtime.CompilerServices;
using System.Text.Json;

namespace BlueHead.Desktop;

// Keep inline labels next to their actions while reserving space for ellipsis.
public sealed class InlineWidthConverter : IMultiValueConverter
{
    public object Convert(object[] values, Type targetType, object parameter, CultureInfo culture)
    {
        if (values[0] is not double width || width <= 0) return double.PositiveInfinity;
        var visible = values.Length < 3 || values[2] is true;
        var reserve = visible && values[1] is double actionWidth ? actionWidth + double.Parse(parameter.ToString()!, CultureInfo.InvariantCulture) : 0;
        return Math.Max(0, width - reserve);
    }
    public object[] ConvertBack(object value, Type[] targetTypes, object parameter, CultureInfo culture) => throw new NotSupportedException();
}

public static class ProjectLinks
{
    public static Uri Repository { get; } = new("https://github.com/tingol666/BlueHead");
}

public sealed class DeviceCard : INotifyPropertyChanged
{
    public string Key { get; init; } = "";
    private string name = "移动设备", media = "媒体编码尚未协商", connection = "已连接", call = "", details = "已连接";
    private double volume = 100, uplink = 100;
    private bool answer, hangup, mixing = true;
    private string playbackNotice = "", mediaAction = "在电脑播放";
    private bool selectMedia, selectedMedia, detailsOpen;
    public bool IsMediaSelected { get => selectedMedia; set { selectedMedia = value; Changed(); } }
    public bool DetailsOpen { get => detailsOpen; set { detailsOpen = value; Changed(); } }
    public string PlaybackNotice { get => playbackNotice; set { playbackNotice = value; Changed(); Changed(nameof(HasPlaybackNotice)); } }
    public bool HasPlaybackNotice => playbackNotice.Length > 0;
    public bool CanSelectMedia { get => selectMedia; set { selectMedia = value; Changed(); } }
    public string MediaAction { get => mediaAction; set { mediaAction = value; Changed(); } }
    public string Name { get => name; set { name = value; Changed(); } }
    public string MediaDescription { get => media; }
    public string LiveDetails { get => details; set { details = value; Changed(); } }
    public string Media { get => media; set { media = value; Changed(nameof(MediaDescription)); } }
    public string Connection { get => connection; set { connection = value; Changed(nameof(MediaDescription)); } }
    public string CallDescription { get => call; set { call = value; Changed(); Changed(nameof(CallVisible)); } }
    public bool CallVisible => answer || hangup || call.Contains("失败") || call.Contains("重连");
    public double Volume { get => volume; set { if (Math.Abs(volume - value) < .01) return; volume = value; Changed(); } }
    public double UplinkVolume { get => uplink; set { if (Math.Abs(uplink - value) < .01) return; uplink = value; Changed(); } }
    public bool CanAnswer { get => answer; set { answer = value; Changed(); Changed(nameof(CallVisible)); } }
    public bool CanHangup { get => hangup; set { hangup = value; Changed(); Changed(nameof(CallVisible)); } }
    public bool CanMix { get => mixing; set { mixing = value; Changed(); } }
    public event PropertyChangedEventHandler? PropertyChanged;
    private void Changed([CallerMemberName] string? property = null) => PropertyChanged?.Invoke(this, new(property));
}
public record AudioChoice(int? Index, string Name, string? EndpointId = null) { public override string ToString() => Name; }
public record AdapterChoice(string Transport, string Name) { public override string ToString() => Name; }
public sealed class DesktopState
{
    public ObservableCollection<DeviceCard> Devices { get; } = [];
    public ObservableCollection<AudioChoice> Outputs { get; } = [];
    public ObservableCollection<AudioChoice> Inputs { get; } = [];
}
public sealed class EngineSettings
{
    public string mode { get; set; } = "advanced";
    public string transport { get; set; } = "";
    public string codec { get; set; } = "auto";
    public string media_mode { get; set; } = "mix";
    public string name { get; set; } = "BlueHead PC";
    public int buffer_ms { get; set; } = 60;
    public int call_buffer_ms { get; set; } = 30;
    public int maximum_devices { get; set; } = 3;
    public int? output_device { get; set; }
    public int? input_device { get; set; }
    public string? output_endpoint_id { get; set; }
    public string? input_endpoint_id { get; set; }
    public bool calls { get; set; } = true;
    public bool ldac_enabled { get; set; }
    public bool microphone_muted { get; set; }
    public double playback_gain { get; set; } = 1;
    public double microphone_gain { get; set; } = 1;
    public double system_gain { get; set; } = 1;
    public bool system_audio_enabled { get; set; }
    public Dictionary<string, DeviceVolumes> device_volumes { get; set; } = [];
}
public sealed class DeviceVolumes
{
    public double down { get; set; } = 1;
    public double up { get; set; } = 1;
}
public sealed class UiSettings
{
    public bool Tray { get; set; } = true;
    public bool AutoStart { get; set; }
}
public static class JsonValue
{
    public static string Text(this JsonElement json, string name, string fallback = "") =>
        json.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() ?? fallback : fallback;
    public static int? Endpoint(this JsonElement json, string name) =>
        json.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetInt32() : null;
    public static double Number(this JsonElement json, string name) =>
        json.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetDouble() : 0;
    public static bool Flag(this JsonElement json, string name) =>
        json.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.True;
}
