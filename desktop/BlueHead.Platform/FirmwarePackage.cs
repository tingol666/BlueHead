using System.ComponentModel;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Text;

namespace BlueHead.Platform;

/// <summary>Static, bounded extraction only. Installers, DLLs and custom actions are never run.
/// Extracted bytes remain untrusted until the native signed-catalog verifier accepts them.</summary>
public sealed class FirmwarePackage : IDisposable
{
    private const long MaxPackage = 256L * 1024 * 1024, MaxExpanded = 128L * 1024 * 1024, MaxFile = 8L * 1024 * 1024;
    private readonly HashSet<string> wanted, found = new(StringComparer.OrdinalIgnoreCase);
    private long expanded, resourceBytes;
    private int entries, containers;
    public string DirectoryPath { get; }
    public int ExtractedCount => found.Count;
    public FirmwarePackage(string parent, IEnumerable<string> names)
    {
        wanted = new(names, StringComparer.OrdinalIgnoreCase);
        if (wanted.Count is 0 or > 64 || wanted.Any(name => !SafeName(name) || name.IndexOfAny(new[] { '/', '\\' }) >= 0))
            throw new IOException("固件文件清单无效。");
        RejectLinks(parent);
        Directory.CreateDirectory(parent);
        DirectoryPath = Path.Combine(Path.GetFullPath(parent), "import-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(DirectoryPath);
    }
    private static bool SafeName(string name) => name.Length is > 0 and <= 240 && !Path.IsPathRooted(name) &&
        name.IndexOfAny(new[] { ':', '\0', '\r', '\n' }) < 0 && name.Replace('\\', '/').Split('/').All(part => part.Length > 0 && part != "." && part != "..");
    private static void RejectLinks(string path)
    {
        for (var item = new DirectoryInfo(Path.GetFullPath(path)); item != null; item = item.Parent)
            if (item.Exists && (item.Attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("固件路径不能经过链接目录。");
        if (File.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new IOException("固件文件不能是链接。");
    }
    private string? Target(string archiveName, long length)
    {
        if (++entries > 8192 || !SafeName(archiveName) || length < 0) throw new IOException("驱动包的目录结构或大小无效。");
        string name = archiveName.Replace('\\', '/').Split('/').Last();
        if (!wanted.Contains(name)) return null;
        if (length is 0 or > MaxFile || expanded + length > MaxExpanded || !found.Add(name))
            throw new IOException("驱动包含重复或过大的固件文件，请选择更具体的驱动包。");
        expanded += length;
        return Path.Combine(DirectoryPath, name);
    }
    private static void CopyBounded(Stream input, Stream output, long maximum)
    {
        var buffer = new byte[32768]; long total = 0;
        for (int count; (count = input.Read(buffer, 0, buffer.Length)) > 0;)
        {
            total += count;
            if (total > maximum) throw new IOException("解压数据超过限制。");
            output.Write(buffer, 0, count);
        }
    }
    public void Extract(string path)
    {
        path = Path.GetFullPath(path); RejectLinks(path);
        if (Directory.Exists(path))
        {
            var folders = new Queue<string>(); folders.Enqueue(path);
            while (folders.Count > 0)
            {
                string folder = folders.Dequeue(); RejectLinks(folder);
                foreach (var entry in Directory.EnumerateFileSystemEntries(folder))
                {
                    RejectLinks(entry);
                    if (++entries > 8192) throw new IOException("驱动目录文件过多。");
                    if (Directory.Exists(entry)) { folders.Enqueue(entry); continue; }
                    string? target = Target(Path.GetFileName(entry), new FileInfo(entry).Length);
                    if (target != null) { using var input = File.OpenRead(entry); using var output = new FileStream(target, FileMode.CreateNew); CopyBounded(input, output, MaxFile); }
                }
            }
            return;
        }
        if (!File.Exists(path) || new FileInfo(path).Length > MaxPackage) throw new IOException("驱动包缺失或超过 256 MB；可导入已解压的原厂驱动目录。");
        switch (Path.GetExtension(path).ToLowerInvariant())
        {
            case ".zip": ExtractZip(path); break;
            case ".cab": ExtractCab(path); break;
            case ".msi": ExtractMsi(path); break;
            case ".exe": ExtractWrapper(path); break;
            default: throw new IOException("请选择 ZIP、CAB、MSI、静态自解压包或已解压的原厂驱动目录。");
        }
    }
    private void ExtractZip(string path)
    {
        using var archive = new ZipArchive(File.OpenRead(path), ZipArchiveMode.Read);
        foreach (var entry in archive.Entries)
        {
            if (entry.FullName.EndsWith("/", StringComparison.Ordinal)) continue;
            if (((entry.ExternalAttributes >> 16) & 0xf000) == 0xa000) throw new IOException("驱动包不能包含符号链接。");
            string? target = Target(entry.FullName, entry.Length);
            if (target == null) continue;
            using var input = entry.Open(); using var output = new FileStream(target, FileMode.CreateNew);
            CopyBounded(input, output, entry.Length);
            if (output.Length != entry.Length) throw new IOException("固件文件解压不完整。");
        }
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CabFile { public IntPtr Name; public uint Size, Error; public ushort Date, Time, Attributes; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string Target; }
    [StructLayout(LayoutKind.Sequential)] private struct CabExtracted { public IntPtr Target, Source; public uint Error, Flags; }
    private delegate uint CabCallback(IntPtr context, uint notification, UIntPtr first, UIntPtr second);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupIterateCabinetW(string path, uint reserved, CabCallback callback, IntPtr context);
    private void ExtractCab(string path)
    {
        Exception? failure = null;
        CabCallback callback = (_, notification, first, _) =>
        {
            try
            {
                if (notification == 0x11) // SPFILENOTIFY_FILEINCABINET
                {
                    var pointer = new IntPtr(unchecked((long)first.ToUInt64()));
                    var file = Marshal.PtrToStructure<CabFile>(pointer);
                    string? target = Target(Marshal.PtrToStringUni(file.Name) ?? "", file.Size);
                    if (target == null) return 2; // FILEOP_SKIP
                    if (target.Length >= 260) throw new IOException("固件暂存路径过长。");
                    file.Target = target; Marshal.StructureToPtr(file, pointer, false); return 1;
                }
                if (notification == 0x12) throw new IOException("不支持跨文件分卷驱动包。");
                if (notification == 0x13)
                {
                    var file = Marshal.PtrToStructure<CabExtracted>(new IntPtr(unchecked((long)first.ToUInt64())));
                    if (file.Error != 0) throw new IOException("CAB 固件解压失败。");
                }
                return 0;
            }
            catch (Exception error) { failure = error; return 0; }
        };
        bool success = SetupIterateCabinetW(path, 0, callback, IntPtr.Zero);
        GC.KeepAlive(callback);
        if (failure != null) throw new IOException("无法静态提取 CAB 固件。", failure);
        if (!success) throw new Win32Exception(Marshal.GetLastWin32Error(), "CAB 文件无法提取。");
    }
    [DllImport("msi.dll", CharSet = CharSet.Unicode)] private static extern uint MsiOpenDatabaseW(string path, IntPtr mode, out uint database);
    [DllImport("msi.dll", CharSet = CharSet.Unicode)] private static extern uint MsiDatabaseOpenViewW(uint database, string query, out uint view);
    [DllImport("msi.dll")] private static extern uint MsiViewExecute(uint view, uint record);
    [DllImport("msi.dll")] private static extern uint MsiViewFetch(uint view, out uint record);
    [DllImport("msi.dll")] private static extern uint MsiRecordReadStream(uint record, uint field, byte[] bytes, ref uint size);
    [DllImport("msi.dll")] private static extern uint MsiCloseHandle(uint handle);
    private void ExtractMsi(string path)
    {
        uint database = 0, view = 0;
        try
        {
            if (MsiOpenDatabaseW(path, IntPtr.Zero, out database) != 0 || MsiDatabaseOpenViewW(database, "SELECT `Data` FROM `_Streams`", out view) != 0 || MsiViewExecute(view, 0) != 0)
                throw new IOException("无法读取 MSI 的静态驱动资源。");
            uint status;
            while ((status = MsiViewFetch(view, out uint record)) == 0)
            {
                try
                {
                    if (++containers > 256) throw new IOException("驱动资源过多。");
                    var buffer = new byte[32768]; uint count = (uint)buffer.Length;
                    if (MsiRecordReadStream(record, 1, buffer, ref count) != 0) throw new IOException("MSI 资源读取失败。");
                    if (count < 4 || Encoding.ASCII.GetString(buffer, 0, 4) != "MSCF") continue;
                    string cab = Path.Combine(DirectoryPath, "container-" + containers + ".cab");
                    using (var output = new FileStream(cab, FileMode.CreateNew))
                    {
                        do
                        {
                            resourceBytes += count;
                            if (output.Length + count > MaxExpanded || resourceBytes > MaxPackage) throw new IOException("MSI 资源过大。");
                            output.Write(buffer, 0, (int)count); count = (uint)buffer.Length;
                            if (MsiRecordReadStream(record, 1, buffer, ref count) != 0) throw new IOException("MSI 资源读取失败。");
                        } while (count > 0);
                    }
                    try { ExtractCab(cab); } finally { File.Delete(cab); }
                }
                finally { MsiCloseHandle(record); }
            }
            if (status != 259) throw new IOException("MSI 静态资源遍历失败。");
        }
        finally { if (view != 0) MsiCloseHandle(view); if (database != 0) MsiCloseHandle(database); }
    }
    private void ExtractWrapper(string path)
    {
        // Only embedded CAB resources are interpreted. No LoadLibrary, shell or installer execution.
        using var input = File.OpenRead(path);
        var bytes = new byte[input.Length]; CopyInto(input, bytes);
        for (int offset = 0; offset <= bytes.Length - 36; offset++)
        {
            if (bytes[offset] != 'M' || bytes[offset + 1] != 'S' || bytes[offset + 2] != 'C' || bytes[offset + 3] != 'F') continue;
            uint length = BitConverter.ToUInt32(bytes, offset + 8);
            if (length < 36 || length > MaxExpanded || length > bytes.Length - offset) continue;
            if (++containers > 32) throw new IOException("安装包中的静态资源过多。");
            string cab = Path.Combine(DirectoryPath, "container-" + containers + ".cab");
            using (var output = new FileStream(cab, FileMode.CreateNew)) output.Write(bytes, offset, (int)length);
            try { ExtractCab(cab); } finally { File.Delete(cab); }
            offset += (int)length - 1;
        }
        if (containers == 0) throw new IOException("此安装包不提供可静态提取的 CAB。请先使用原厂解压功能，或选择原厂驱动引导。");
    }
    private static void CopyInto(Stream input, byte[] bytes)
    {
        int offset = 0;
        while (offset < bytes.Length) { int count = input.Read(bytes, offset, bytes.Length - offset); if (count == 0) throw new IOException("安装包不完整。"); offset += count; }
    }
    public void Dispose()
    {
        // This directory is a fresh GUID owned by this operation; never remove the selected package.
        try
        {
            RejectLinks(DirectoryPath);
            if (!Directory.Exists(DirectoryPath)) return;
            foreach (string entry in Directory.EnumerateFileSystemEntries(DirectoryPath))
            {
                RejectLinks(entry);
                if (Directory.Exists(entry)) throw new IOException("暂存区出现意外目录。");
                File.Delete(entry);
            }
            Directory.Delete(DirectoryPath);
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
