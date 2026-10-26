# 构建 BlueHead 2.0.1

目标：Windows 11 x64。本文说明源码构建、检查和发布打包流程；构建脚本不会自动更换驱动、连接移动设备或发布GitHub Release。

## 安装目录与1.x迁移限制

2.0.1安装器仅支持新建或已经为空的目标目录，默认`%LocalAppData%\Programs\BlueHead-2`。目录页离开时及真正写入前都会检查，已有文件、子目录、不可检查或重解析目标均拒绝，不递归清理，不按旧manifest删除文件。这样避免1.x现代.NET、PyAV/NumPy等组件残留进2.0。**覆盖升级、修复安装及后续2.x原地升级目前均不支持。**

使用1.x安装版时，先备份`%LocalAppData%\BlueHead\data`、从托盘退出，并在安装2.0前完成旧版卸载。旧卸载器会按提示恢复系统蓝牙，因此由使用者安排该真实驱动步骤；不要安装2.0后再运行旧卸载器。安装版的配对/设置仍使用原全局目录和格式，不因换程序目录而自动迁移或删除。已有便携1.2.4无需卸载，可原样保留回退；绿色2.0同样解压到新空目录，在两版均完全退出后再备份/复制原`data`，不同时运行。后续更换安装版也应先退出、备份并卸载旧安装，再选新空目录。

安装注册键、卸载键和开始菜单使用独立`BlueHead2`名称，系统显示为`BlueHead 2`。新卸载器不清理1.x的注册/快捷方式，且只有共享`Run\BlueHead`值精确指向当前安装的已知`--tray`命令时才删除它。未知或另一目录拥有的自启值保留。卸载仅删除该版本生成的确切程序文件，用户目录和驱动恢复状态按既有保护流程处理。

## 工具链

- Python3.13及虚拟环境。
- `global.json`选择的.NET SDK10.0.402，用于现代C#编译；产物目标为.NET Framework4.8 x64。
- MSYS2 UCRT64 GCC/G++、bash、make和构建FFmpeg所需工具。
- NSIS3用于安装包。

首次恢复依赖需要联网。上游源码/摘要保存在vendor，NuGet各项目使用packages.lock.json。开发锁文件中PyAV、NumPy和sounddevice可用于历史回归与离线对照，不等于发布包运行时需要它们。

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
$env:PATH = 'C:\msys64\ucrt64\bin;C:\msys64\usr\bin;' + $env:PATH
```

## 原生模块

主要顺序如下；完整发布脚本还会处理进程排除采集、驱动库、启动器等模块。只从固定源码构建，不能用来源不明的DLL替换。

```powershell
.venv\Scripts\python.exe tools/build_ffmpeg.py --native-only
.venv\Scripts\python.exe tools/build_audio.py
.venv\Scripts\python.exe tools/build_voice.py
.venv\Scripts\python.exe tools/build_device.py
.venv\Scripts\python.exe tools/build_ldac_media.py
.venv\Scripts\python.exe tools/build_loopback.py
.venv\Scripts\python.exe tools/build_runtime.py
```

FFmpeg直接供原生媒体/语音模块调用，发布只保留avcodec、avutil和swresample。`--native-only`不需要修改PyAV wheel；开发对照环境与发布依赖分离。对应源码、构建配置和许可必须随可再分发源码材料保留。

C++ 运行时使用固定 nlohmann/json 3.12.0 解析控制消息；构建时核对 vendored 头文件、许可证及 libusb 源码归档的摘要。EXE 的版本资源、原生握手与桌面版本均由项目版本生成。

SpeexDSP固定版本及摘要用于有状态SRC；miniaudio0.11.25仅编译WASAPI；liblc3固定v1.1.3；SpanDSP PLC作为独立可替换动态库。不要为减少文件数把有许可证边界的库随意静态合并。libusb/libwdi继续保留其源码、修改及替换条件。

## 桌面与驱动工具

```powershell
$env:DOTNET_CLI_HOME = Join-Path $PWD 'work/dotnet-home'
$env:NUGET_PACKAGES = Join-Path $PWD 'work/nuget'
dotnet restore desktop/BlueHead.Desktop/BlueHead.Desktop.csproj --locked-mode
dotnet restore desktop/BlueHead.DriverHelper/BlueHead.DriverHelper.csproj --locked-mode
dotnet publish desktop/BlueHead.Desktop/BlueHead.Desktop.csproj -c Release --no-restore -o work/publish-desktop-201
dotnet publish desktop/BlueHead.DriverHelper/BlueHead.DriverHelper.csproj -c Release --no-restore -o work/publish-helper-201
```

不能传`--self-contained`、现代.NET运行时RID或trimming参数。所有项目固定x64/Prefer32Bit=false；保留生成的exe.config、所需NuGet依赖及native DLL。官方net48 reference assemblies仅用于编译，不进入运行包。WPF/WinForms/COM使用目标Windows自带的Framework，不通过手工删DLL瘦身。

桌面publish目录本身不包含完整蓝牙后台，不能直接当作用户安装包。调试时设置`BLUEHEAD_SOURCE_ROOT`为源码绝对路径，界面会启动 `bluehead/assets/BlueHead.Runtime.exe --source-root ...`，由它启动 `.venv/Scripts/python.exe launch.py --engine` 控制进程；需先构建同版本的原生后台。`BLUEHEAD_DATA_DIR`可指定独立测试数据，避免接触日常配对配置。

## 检查与本地打包

```powershell
.venv\Scripts\python.exe -m pytest tests -q --tb=short
.venv\Scripts\python.exe -m ruff check bluehead tools tests launch.py engine_launch.py
.venv\Scripts\python.exe tools/build_release.py --output ..\releases\2.0.1
```

构建输出需使用新目录，不能覆盖运行中的日常程序。原生时钟长测、频率/幅度门槛和逐用例JSON的复现命令见[AUDIO_AUDIT_2.0.md](AUDIO_AUDIT_2.0.md)；它们使用虚拟时间及真实DLL，不打开声卡或蓝牙。依赖清点、离线后台自检、微基准与需使用者安排的硬件探针，其运行范围见[DIAGNOSTICS.md](DIAGNOSTICS.md)。完整验收矩阵见[TESTING.md](TESTING.md)。

### 必须先写同版本VALIDATION

`build_release.py`生成程序目录后、运行`finalize_release.py`前，必须在同版本发布目录创建`VALIDATION.md`，本例为`..\releases\2.0.1\VALIDATION.md`。它必须记录此次实际执行的结果，不能复制历史通过数量，也不能把待做的实机检查写成通过：

- 发布版本、源码修订/工作区状态、构建时间、目标系统和实际工具链版本。
- 完整回归、Ruff、Framework兼容层、纯UI、独立helper只读检查，以及冻结后台离线检查的命令、结果和报告位置；未运行项明确标注。
- 时钟长测、音频质量门槛与关键生命周期/隔离检查的结果；软件缓冲或处理耗时不得当作端到端延迟。
- 程序目录的版本、文件与依赖清单、体积及检查摘要；缺陷、已知限制、已确认的实际使用结果、尚未覆盖的验收项目和回退方式。最终归档校验和由下一步另写`SHA256SUMS.txt`，不要求把归档自身的摘要写进其内含文档。

对完整包运行适用的离线检查，确认验证记录与该包一致，再执行最终归档：

```powershell
.venv\Scripts\python.exe tools/finalize_release.py ..\releases\2.0.1 --nsis 'C:\Program Files (x86)\NSIS\makensis.exe'
```

最终打包生成安装EXE、Portable ZIP、Source ZIP、`SHA256SUMS.txt`与验证说明；`finalize_release.py`检查`VALIDATION.md`存在，并把它放进绿色版。更改程序或验证记录后须重新归档，保持内外记录及摘要一致。存在记录本身不表示其中所有场景都已实测，最终大小、校验和和检查数量以对应产物和报告为准。

发布工具必须确认冻结后台不收集av/NumPy/OpenBLAS/sounddevice/PortAudio，不携带现代.NET自包含运行库；同时保留所有实际使用的codec、Framework依赖、WinRT、驱动恢复与许可证。不要为减包删COM/WPF组件或恢复工具。

公开源码使用允许列表，不包含data、local、work、.venv、bin/obj、私钥、日志或驱动恢复备份。测试程序和演示截图不能冒充真实连接结果。上传前审阅README、第三方说明、同版本VALIDATION及归档内容，确认版本、功能介绍、依赖材料和验证范围一致。
