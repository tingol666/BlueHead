# 诊断工具与运行范围

先区分依赖检查、离线算法检查和真实硬件测试。前两类通过，不表示蓝牙链路、
声音热切换或驱动更换已经通过验收。开发脚本从源码根目录运行；需要数值处理的
离线基准和测试音探针使用开发环境依赖，不要求发布包安装 NumPy/PyAV。

## 只读清点

```powershell
.venv\Scripts\python.exe tools\diagnose.py
```

`diagnose.py` 初始化当前原生媒体 codec，检查编译的语音 codec，枚举 USB HCI
适配器数量、原生 WASAPI 输入/输出端点数量及 Windows 已配对音频来源数量。
它不打开真实音频流、不播放、不采集麦克风、不连接移动设备、不 claim/reset
蓝牙适配器，也不修改驱动。端点和设备信息只在内存中计数，输出不含设备名称、
原始地址、配对密钥或原始音频。

LDAC 报告只检查实验性库文件是否存在，明确标为未执行，不把文件存在当作解码
或互通成功。报告中的 `passed` 表示列出的依赖及清点操作完成；设备数为零不代表
声卡或适配器已经适合实际接收。失败只输出错误类型，便于安全分享；排障仍需结合
完整版本和缺失的检查项。

## 离线检查，不打开真实设备

先为自己的报告创建工作目录：

```powershell
New-Item -ItemType Directory -Path work -Force
.venv\Scripts\python.exe engine_launch.py --offline-audio-check work\offline-audio-check.json
.venv\Scripts\python.exe tools\realtime_benchmark.py work\realtime-benchmark.json
.venv\Scripts\python.exe tools\voice_stress.py --minutes 1 --report work\voice-stress.json
```

从完整发布包根目录可使用 `app\engine\BlueHead.Engine.exe --offline-audio-check 报告路径`。
离线自检使用固定的合成压缩样本和原生内存端点；不会调用 USB 接收或创建真实
播放/麦克风流。报告目录应先存在。完整协议回归、UI 自检和包检查见
[TESTING.md](TESTING.md)。

`realtime_benchmark.py` 测原生回调与 SRC 的离线调用成本，`voice_stress.py`
按算法实际速度处理指定的模拟语音时长。它们可能占用 CPU，但不访问真实声卡、
麦克风或蓝牙。微基准耗时和缓冲配置均不能当作蓝牙端到端延迟。

## 使用者安排的真实硬件探针

以下工具不会在诊断或默认测试中自动执行。运行它们就是主动进行硬件测试，应
先确认目标和当前系统状态。这里列出用途和副作用；各探针实际运行情况见同版本验证记录。

| 工具 | 用途 | 真实副作用 |
| --- | --- | --- |
| `tools/hardware_probe.py` | 初始化显式选择的 USB 蓝牙控制器，读取版本、特性和 USB 语音接口信息 | 打开并初始化所选适配器；可能中断该适配器原有连接；不主动连接手机或调用麦克风 |
| `tools/usb_voice_probe.py` | 检查显式选择的 USB/语音接口 | 会打开所选 USB 控制器，不是只读枚举；需按脚本要求传入 transport 与报告路径 |
| `app\engine\BlueHead.Engine.exe --system-audio-check 报告路径` | 发布包的进程排除采集初始化/关闭检查 | 访问真实 Windows 电脑声音采集；不播放测试音，不验证完整混音质量 |
| `tools/loopback_probe.py --run` | 比较父/子进程合成音的包含与排除，检查数字返送排除路径 | 默认输出播放两种低音量测试音，并短暂在内存采集所包含的电脑声音；不打开麦克风、USB 或蓝牙 |

USB 探针需停止 BlueHead 接收，并使用已明确选择、可用于专用接收的适配器。
不要把说明中的占位 transport 直接当作真实设备；这些脚本不会替换或恢复驱动。
`hardware_probe.py` 等底层探针的日志可能包含原始 HCI 信息和地址，不能未经检查
直接公开上传。

原生进程排除测试可由使用者在 Windows build 20348 或更高版本上运行：

```powershell
.venv\Scripts\python.exe tools\loopback_probe.py --run --report work\loopback-native.json
```

测试前暂停其它应用的播放，并使用合适的默认输出；默认每阶段测量 1.2 秒。
`--seconds` 可选择 0.8–3 秒。不传 `--run` 不会启动测试音或采集。测试使用原生
Mixer、WASAPI AudioOutput 和进程排除采集，Python 只在开发线程生成合成音和计算
频率幅度；不使用 PortAudio。报告只保存数值及错误类型，不保存音频文件。
其它应用的声音可能影响幅度阈值，因此报告失败也需结合运行环境判断。

## 已归档的旧脚本

旧的音频切换、接收线程双向探针、Beta 4 混合探针和硬编码数据目录的 HFP 探针
已从 `tools/` 移除，旧副本已随历史构建备份清理，不参与当前源码分发或构建。
这些历史脚本依赖旧 PortAudio、旧 mSBC 接口或过时日志过滤，不能作为 2.0.0
诊断使用，也不应复制回公开工具目录运行。

分享报告前仍应确认未包含配对目录、恢复备份、设备地址、个人路径或原始日志。
应用中的曲目内容只供实时 UI 使用，不应随诊断报告导出。
