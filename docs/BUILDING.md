# 从源码构建 BlueHead

目标为 Windows 10 / 11 x64。产品桌面基于 .NET Framework 4.8；构建使用现代 SDK 编译 C#，不把现代 .NET Runtime 放进发布包。脚本不会自动切换蓝牙驱动、连接移动设备或发布 GitHub。

## 工具链

- Python 3.13，`global.json` 固定的 .NET SDK，MSYS2 UCRT64 GCC/G++、bash、make、diffutils。
- NSIS 3 用于生成安装器。
- 首次恢复 Python、NuGet 依赖需要联网；固定上游源码、摘要和许可材料在 `vendor/`。

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
$env:PATH = 'C:\msys64\ucrt64\bin;C:\msys64\usr\bin;' + $env:PATH
.venv\Scripts\python.exe tools/sync_version.py --check
.venv\Scripts\python.exe tools/build_all_native.py
```

`CC`／`CXX` 可指定编译器，`MAKE` 可指定 GNU make 的绝对路径。构建依次生成裁剪 FFmpeg、音频／语音／设备库、LDAC、LHDC 5.0、电脑声音采集、驱动库、诊断库和原生后台。除固定外部 libusb（附源码与摘要）外，BlueHead 自有 EXE/DLL 与 FFmpeg 产物不提交 Git。通用包不附带厂商固件镜像；签名获取目录及许可材料随源码保留。

## 桌面与驱动工具

```powershell
dotnet restore desktop/BlueHead.Desktop/BlueHead.Desktop.csproj --locked-mode
dotnet restore desktop/BlueHead.DriverHelper/BlueHead.DriverHelper.csproj --locked-mode
dotnet publish desktop/BlueHead.Desktop/BlueHead.Desktop.csproj -c Release --no-restore -o work/publish-desktop
dotnet publish desktop/BlueHead.DriverHelper/BlueHead.DriverHelper.csproj -c Release --no-restore -o work/publish-helper
```

保留 `exe.config` 和发布依赖；不要使用 `--self-contained` 或 trimming。只构建桌面还不包含完整后台。源码调试可设置 `BLUEHEAD_SOURCE_ROOT` 为检出目录、`BLUEHEAD_DATA_DIR` 为独立数据目录，再运行构建好的桌面。原生 Runtime 会启动 `.venv/Scripts/python.exe launch.py --engine`，不要与日常程序共用运行中的适配器。

## 软件检查

```powershell
.venv\Scripts\python.exe -m ruff check bluehead tools launch.py engine_launch.py
.venv\Scripts\python.exe engine_launch.py --offline-audio-check work/offline-native.json
```

桌面 `--self-test 报告目录` 检查界面演示状态；更多边界见 [TESTING](TESTING.md)。本地维护回归测试不属于公开构建输入。`native/testing/runtime_*` 和桌面的内置检查代码是产品离线诊断的依赖，保留它们才能构建同等产品。

## 安装包与绿色版

```powershell
.venv\Scripts\python.exe tools/build_release.py --output ../releases/2.1.6
```

输出必须是新目录。此入口完成原生构建、许可证清点、桌面／驱动工具发布、冻结控制后台和装配；不会部署到日常目录。已构建各部分时，可显式传入 `--assemble-only --desktop ... --helper ... --engine ...`，输入必须都是同一源码版本。

在输出目录写入 `VALIDATION.md`，记录真实执行的命令、结果、构建版本、包检查与尚未覆盖的设备验收。不能把历史通过数量、虚拟时钟或待做项目冒充当前实测。然后：

```powershell
.venv\Scripts\python.exe tools/audit_package.py ../releases/2.1.6/BlueHead --report work/package-audit.json
.venv\Scripts\python.exe tools/finalize_release.py ../releases/2.1.6 --nsis 'C:\Program Files (x86)\NSIS\makensis.exe'
```

生成 Setup EXE、Portable ZIP、Source ZIP、验证说明及 `SHA256SUMS.txt`。绿色版加 `portable.flag`，数据保存在自身 `data/`；安装版使用 `%LocalAppData%\BlueHead\data`。源码 ZIP 与 Git 当前树采用相同公开清单，`tools/audit_sources.py` 检查边界。私钥、驱动备份、配对、工具链、本地测试和缓存不发布。

安装器要求新的空目录，目前不支持覆盖安装。升级先从托盘退出、备份数据，再卸载旧安装并选空目录；绿色版解压到新目录后，只迁移自己的数据，勿混用程序文件。旧安装器卸载可能需要恢复驱动，请先完成旧版卸载。卸载按确切文件清单移除程序，不按未知历史 manifest 递归清理其它文件。

## 修改原则

版本只编辑 `pyproject.toml`，运行 `tools/sync_version.py` 同步 Python 与桌面常量。不要修改固定 vendor 原始文件来绕过摘要校验；在构建适配或自有桥接层记录变化。LGPL 等独立组件保留源码、许可及动态替换条件，不能为了减文件数随意静态合并。
