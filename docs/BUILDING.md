# Windows x64 构建

使用 Windows 11 x64、Python 3.13、.NET SDK 10.0.402 和 MSYS2 UCRT64 GCC/G++。MSYS2 还需要 bash、make、diffutils；安装包使用 NSIS 3。源码不要求安装 Visual Studio。构建本身不更换蓝牙驱动。

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
$env:PATH = 'C:\msys64\ucrt64\bin;C:\msys64\usr\bin;' + $env:PATH
.venv\Scripts\python.exe tools/build_release.py --output ..\releases\1.2.4
```

`gcc`、`dotnet` 和 NSIS 必须来自可信工具链。`global.json` 固定 .NET SDK；NuGet 包用各项目 `packages.lock.json` 固定。首次构建下载 Python/NuGet 依赖，需要联网；程序运行不需要联网。完整构建依次构建 LDAC、原生混音、进程排除音频采集、libwdi、音频专用 FFmpeg，再发布 C# 和冻结后台。输出目录必须为空，避免覆盖运行中的文件。

`bluehead/assets/libusb-1.0.dll` 是 libusb 官方 1.0.30 x64 发布 DLL，未修改。其来源、SHA256 和对应源码位于 `vendor/libusb`。可替换为同 ABI 的自编译 DLL。

FFmpeg 对应源码为 `vendor/ffmpeg/ffmpeg-9.0.2.tar.xz`，脚本先校验 SHA256，再构建音频组件。PyAV 19.0.2 使用同一 FFmpeg ABI，`build_ffmpeg.py` 把其 PE 导入名适配为可替换的标准 DLL 名称，原 wheel 保存在 `work/pyav-original-wheel`。不要在 PyAV 正在运行时执行此脚本。实际配置和 DLL 摘要保存在 `vendor/ffmpeg/build.json`；本机路径只用于记录构建位置。FFmpeg 源码无需修改。

libwdi 对应固定提交保存在 `vendor/libwdi`，修改由 `build_wdi.py` 应用到构建副本。Windows 10+ 模板引用系统自带 WinUSB，不包含旧版 coinstaller。确切设备的驱动绑定在 C# 中完成。

```powershell
.venv\Scripts\python.exe -m pytest tests -q --tb=short
.venv\Scripts\python.exe -m ruff check bluehead tools tests launch.py engine_launch.py
.venv\Scripts\python.exe tools/finalize_release.py ..\releases\1.2.4 --nsis 'C:\Program Files (x86)\NSIS\makensis.exe'
```

最后一步生成安装 EXE、Portable ZIP、Source ZIP 和 SHA256SUMS。公开文件使用允许列表生成，不包含 `local`、`data`、`.venv`、`work`、`archive` 或日志。Portable ZIP 自带 `portable.flag`；安装包没有该标记，使用 LocalAppData 保存数据。

调试界面时可设置 `BLUEHEAD_SOURCE_ROOT` 为源码绝对路径，随后 `dotnet run --project desktop/BlueHead.Desktop`。界面会使用 `.venv/Scripts/python.exe launch.py --engine`，无需反复冻结 Python。`BLUEHEAD_DATA_DIR` 可以指定独立测试数据目录，优先于绿色版或安装版的默认目录。

公开 GitHub 时上传 Source ZIP 解压后的内容；创建 `v1.2.4` 正式 Release 并附上同版本安装包、Portable ZIP、Source ZIP、SHA256SUMS 和 VALIDATION.md。不要上传本机 `source` 目录里被 Git 忽略的构建缓存或私有数据。


## 原生回调与包体积

`tools/build_audio.py` 先校验 `vendor/speexdsp/origin.json` 中的固定文件摘要，再将原生混音、PortAudio 回调和 SpeexDSP 1.2.1 编入 `bluehead_audio.dll`。Windows x64 使用 SSE/SSE2 基线指令，不要求 AVX；上游源码不改动，使用 BSD-3-Clause 许可。质量级别固定为 10，格式状态在音频流启动前创建。

发布使用 self-contained WPF 和冻结后台。只收集运行所需的模块，排除 Bumble 示例/工具和 PyAV 开发数据；保留原生媒体/语音编解码器、WinRT 和驱动恢复依赖。.NET 保留中英文资源及完整运行库，不启用与 WPF/COM 不兼容的程序集裁剪。源码中的原生 DLL 为构建/测试所需；发布包只装载实际使用的 DLL。ZIP 使用标准 Deflate，Windows 解压兼容。

版本以 pyproject、Python 包与桌面元数据的自动一致性检查保护；启动器和安装包读取项目版本，界面、帮助及诊断从程序集元数据读取。Release 前先运行所有自动检查，编写实际验证范围，再生成归档和校验和。不要把工作目录或私人状态当成源码上传。
