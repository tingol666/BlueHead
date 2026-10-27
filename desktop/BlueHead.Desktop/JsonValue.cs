using System.Text.Json;

namespace BlueHead.Desktop;

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
