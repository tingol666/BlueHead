# Windows desktop

BlueHead keeps its WPF interface, Windows Forms tray icon and independent driver
helper. The desktop projects target **.NET Framework 4.8, x64**. Windows 10 22H2
and Windows 11 already include a compatible runtime; users do not need to install
the .NET Desktop Runtime used by earlier BlueHead packages.

The build uses a current .NET SDK compiler and official, pinned .NET Framework
reference assemblies. These reference assemblies are build inputs and are not
redistributed. WPF, Windows Forms and COM support come from Windows; their DLLs
must not be manually copied, trimmed or removed from Windows.

## Build

From the repository root, with the SDK selected by `global.json`:

```powershell
$env:DOTNET_CLI_HOME = Join-Path $PWD 'work/dotnet-home'
$env:NUGET_PACKAGES = Join-Path $PWD 'work/nuget'
dotnet restore desktop/BlueHead.Desktop/BlueHead.Desktop.csproj --locked-mode
dotnet restore desktop/BlueHead.DriverHelper/BlueHead.DriverHelper.csproj --locked-mode
dotnet publish desktop/BlueHead.Desktop/BlueHead.Desktop.csproj -c Release --no-restore -o work/publish-desktop-200
dotnet publish desktop/BlueHead.DriverHelper/BlueHead.DriverHelper.csproj -c Release --no-restore -o work/publish-helper-200
```

Do not pass `--self-contained`, a .NET runtime identifier or trimming flags. Keep
the generated `.exe.config` files, all resolved application dependencies and
the native driver library when assembling a complete package. The complete
release also needs the separate Bluetooth/audio engine; a desktop publish folder
alone is not the end-user package.

## Offline checks

```powershell
work/publish-desktop-200/BlueHead.exe --self-test work/desktop-200-self-test
work/publish-helper-200/BlueHead.DriverHelper.exe --self-test
dotnet restore desktop/BlueHead.FrameworkChecks/BlueHead.FrameworkChecks.csproj --locked-mode
dotnet run --project desktop/BlueHead.FrameworkChecks/BlueHead.FrameworkChecks.csproj -c Release --no-restore -- work/framework-200-checks
```

The desktop test renders UI fixtures without creating Bluetooth links. The
helper self-test only enumerates adapters. FrameworkChecks uses its own temporary
records and child processes to check Windows argument quoting, process exit
races, owned-process-tree cleanup, UTF-8, legacy recovery hashes and atomic
record replacement. It does not change drivers, open audio devices or send
Bluetooth traffic. Test executables and reports are not release payloads.

`BlueHead.Platform/FrameworkCompat.cs` contains the small Framework compatibility
boundary. Driver recovery records retain the existing path/hash format. Driver
operations still run in the separate UAC helper against one validated device
instance, with the existing adapter lease and rollback checks. The desktop owns
its engine's Windows job object so timeouts and desktop exit clean up that
process tree without targeting unrelated processes.
