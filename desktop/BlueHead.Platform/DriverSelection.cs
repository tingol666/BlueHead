namespace BlueHead.Platform;

public sealed record DriverCandidate(string Inf, string Description, string Provider, uint Rank, ulong Version, long Date, bool BluetoothClass, uint Flags);
public static class DriverSelection
{
    public static string ExportedInf(string installedInf, IEnumerable<string> exported)
    {
        byte[] Digest(string path) { using var hash = System.Security.Cryptography.SHA256.Create(); using var stream = File.OpenRead(path); return hash.ComputeHash(stream); }
        var expected = Digest(installedInf);
        var files = exported.Take(65).ToArray();
        if (files.Length > 64) throw new IOException("原厂驱动包包含过多安装信息，未更换驱动。");
        var matches = files.Where(path => Digest(path).SequenceEqual(expected)).ToArray();
        if (matches.Length != 1) throw new IOException("导出的原厂驱动包无法唯一匹配原安装信息，未更换驱动。");
        return matches[0];
    }
    // Windows rank first, then the Windows date/version tie breakers. No brand preference.
    public static DriverCandidate? Original(IEnumerable<DriverCandidate> candidates, string currentInf)
        => candidates.Where(item => item.BluetoothClass && item.Rank != uint.MaxValue && (item.Flags & 0x800) == 0
              && !Path.GetFileName(item.Inf).Equals(currentInf, StringComparison.OrdinalIgnoreCase))
            .OrderBy(item => item.Rank).ThenByDescending(item => item.Date).ThenByDescending(item => item.Version)
            .FirstOrDefault();
}
