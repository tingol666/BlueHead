using System.IO.Compression;
using System.Text;
using System.Runtime.InteropServices;
using BlueHead.Platform;

internal static class FirmwareChecks
{
    [DllImport("msi.dll", CharSet = CharSet.Unicode)] private static extern uint MsiOpenDatabaseW(string path, IntPtr mode, out uint database);
    [DllImport("msi.dll", CharSet = CharSet.Unicode)] private static extern uint MsiDatabaseOpenViewW(uint database, string query, out uint view);
    [DllImport("msi.dll")] private static extern uint MsiCreateRecord(uint fields);
    [DllImport("msi.dll", CharSet = CharSet.Unicode)] private static extern uint MsiRecordSetStringW(uint record, uint field, string value);
    [DllImport("msi.dll", CharSet = CharSet.Unicode)] private static extern uint MsiRecordSetStreamW(uint record, uint field, string path);
    [DllImport("msi.dll")] private static extern uint MsiViewExecute(uint view, uint record);
    [DllImport("msi.dll")] private static extern uint MsiDatabaseCommit(uint database);
    [DllImport("msi.dll")] private static extern uint MsiCloseHandle(uint handle);
    private static void MakeMsi(string path, string cab)
    {
        uint database = 0, view = 0, record = 0;
        try
        {
            if (MsiOpenDatabaseW(path, new IntPtr(3), out database) != 0 ||
                MsiDatabaseOpenViewW(database, "INSERT INTO `_Streams` (`Name`, `Data`) VALUES (?, ?)", out view) != 0 ||
                (record = MsiCreateRecord(2)) == 0 || MsiRecordSetStringW(record, 1, "driver.cab") != 0 ||
                MsiRecordSetStreamW(record, 2, cab) != 0 || MsiViewExecute(view, record) != 0 || MsiDatabaseCommit(database) != 0)
                throw new IOException("Cannot create static MSI fixture");
        }
        finally { if (record != 0) MsiCloseHandle(record); if (view != 0) MsiCloseHandle(view); if (database != 0) MsiCloseHandle(database); }
    }
    internal static void Run(string root, IDictionary<string, bool> checks)
    {
        string packageRoot = Path.Combine(root, "firmware-packages"); Directory.CreateDirectory(packageRoot);
        string zip = Path.Combine(packageRoot, "driver.zip");
        using (var archive = new ZipArchive(File.Create(zip), ZipArchiveMode.Create))
        {
            using (var stream = archive.CreateEntry("driver/bluetooth/test.bin").Open()) stream.Write(new byte[] { 1, 2, 3 }, 0, 3);
            using (var stream = archive.CreateEntry("setup.exe").Open()) stream.WriteByte(0);
        }
        string extracted;
        using (var package = new FirmwarePackage(packageRoot, ["test.bin"]))
        {
            package.Extract(zip); extracted = package.DirectoryPath;
            checks["firmware_zip_extracts_only_requested_data"] = package.ExtractedCount == 1 && File.ReadAllBytes(Path.Combine(extracted, "test.bin")).SequenceEqual(new byte[] { 1, 2, 3 }) && !File.Exists(Path.Combine(extracted, "setup.exe"));
        }
        checks["firmware_extraction_removes_only_own_staging"] = !Directory.Exists(extracted) && File.Exists(zip);
        foreach (var mode in new[] { "traversal", "duplicate", "oversize" })
        {
            string bad = Path.Combine(packageRoot, mode + ".zip");
            using (var archive = new ZipArchive(File.Create(bad), ZipArchiveMode.Create))
            {
                var entry = archive.CreateEntry(mode == "traversal" ? "../test.bin" : "test.bin");
                using (var stream = entry.Open())
                {
                    if (mode == "oversize") { var block = new byte[1024]; for (int i = 0; i < 8193; i++) stream.Write(block, 0, block.Length); }
                    else stream.WriteByte(1);
                }
                if (mode == "duplicate") { using var stream = archive.CreateEntry("nested/test.bin").Open(); stream.WriteByte(2); }
            }
            using var package = new FirmwarePackage(packageRoot, ["test.bin"]);
            try { package.Extract(bad); checks["firmware_zip_rejects_" + mode] = false; }
            catch (IOException) { checks["firmware_zip_rejects_" + mode] = !File.Exists(Path.Combine(root, "test.bin")); }
        }
        string cab = Path.Combine(packageRoot, "driver.cab");
        // Minimal uncompressed single-file Microsoft Cabinet; uses the real SetupAPI extractor.
        using (var writer = new BinaryWriter(File.Create(cab), Encoding.ASCII))
        {
            const string name = "test.bin";
            int filesOffset = 44, dataOffset = filesOffset + 16 + name.Length + 1, length = dataOffset + 8 + 3;
            writer.Write(Encoding.ASCII.GetBytes("MSCF")); writer.Write(0); writer.Write(length); writer.Write(0); writer.Write(filesOffset); writer.Write(0);
            writer.Write((byte)3); writer.Write((byte)1); writer.Write((ushort)1); writer.Write((ushort)1); writer.Write((ushort)0); writer.Write((ushort)0); writer.Write((ushort)0);
            writer.Write(dataOffset); writer.Write((ushort)1); writer.Write((ushort)0);
            writer.Write(3); writer.Write(0); writer.Write((ushort)0); writer.Write((ushort)0); writer.Write((ushort)0); writer.Write((ushort)0x20); writer.Write(Encoding.ASCII.GetBytes(name)); writer.Write((byte)0);
            writer.Write(0); writer.Write((ushort)3); writer.Write((ushort)3); writer.Write(new byte[] { 4, 5, 6 });
        }
        using (var package = new FirmwarePackage(packageRoot, ["test.bin"]))
        {
            package.Extract(cab);
            checks["firmware_cab_static_extraction"] = File.ReadAllBytes(Path.Combine(package.DirectoryPath, "test.bin")).SequenceEqual(new byte[] { 4, 5, 6 });
        }
        string msi = Path.Combine(packageRoot, "static.msi"); MakeMsi(msi, cab);
        using (var package = new FirmwarePackage(packageRoot, ["test.bin"]))
        {
            package.Extract(msi);
            checks["firmware_msi_reads_streams_without_actions"] = package.ExtractedCount == 1;
        }
        string wrapper = Path.Combine(packageRoot, "static.exe");
        File.WriteAllBytes(wrapper, Encoding.ASCII.GetBytes("MZ-static-never-executed").Concat(File.ReadAllBytes(cab)).ToArray());
        using (var package = new FirmwarePackage(packageRoot, ["test.bin"]))
        {
            package.Extract(wrapper);
            checks["firmware_wrapper_data_is_not_executed"] = package.ExtractedCount == 1;
        }
    }
}
