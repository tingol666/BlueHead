# BlueHead

**把 Windows 电脑变成移动设备的蓝牙耳机。**

BlueHead is a free, open-source Bluetooth Classic audio receiver and hands-free endpoint for Windows. Listen to a mobile device on your computer and send the computer's microphone back over Bluetooth. No shared Wi-Fi or mobile companion app is required.

安装版和绿色版不附带厂商固件镜像。需要时按提示获取匹配文件或从原厂驱动包导入；验证后持久缓存，之后可离线启动。

[下载已发布稳定版](https://github.com/tingol666/BlueHead/releases/latest) · [使用指南](docs/UI_GUIDE.md) · [构建源码](docs/BUILDING.md) · [验证范围](docs/TESTING.md) · [报告问题](https://github.com/tingol666/BlueHead/issues)

![BlueHead 2.1.3 界面；设备、曲目信息和连接均为演示数据](docs/images/home-demo.png)

## 可以做什么

- 在电脑上收听手机、平板等设备的媒体声音，并通过 HFP 将电脑麦克风发送回设备。
- 同时混合多台设备的媒体，或在当前来源暂停后自动接替另一台已请求播放的设备。
- 对每台设备的接收与麦克风发送分别调节 0%–200% 增益、独立静音，另有总音量。
- 接收期间切换电脑播放设备和麦克风；选择系统默认时跟随默认设备变化。
- 按需把其他电脑应用的声音与实体麦克风混合发送，排除 BlueHead 自己播放的设备声音。
- 在应用内备份原驱动、启用 WinUSB 专用接收或恢复系统蓝牙。
- 适配 iPhone/iPad 标准 AAC/SBC 媒体与 HFP 双向语音。
- 通过 AVRCP 显示媒体信息、控制上一曲/播放/暂停/下一曲，以及同步标准设备音量。
- 支持 HFP LC3-SWB 协商和 32 kHz 超宽带语音，保留 mSBC/CVSD 回退。

## 音频实现的特点

BlueHead 为持续收发重构了 C/C++ 音频核心。设计重点是减少软件造成的停顿、杂音和信号变化，同时保持桌面控制与协议层易于维护。

- **实时音频在原生后台运行**：USB/HCI、媒体解包解码、SCO 双向收发、PCM 桥接、混音与 WASAPI I/O 都由 C/C++ 负责。已经建立的专用音频流不依赖 Python GIL 或界面刷新。
- **一个适配器，一个 USB/HCI 所有者**：控制器初始化、传输与音频路由共用明确的物理所有权；解码工作线程、端点操作与控制消息分开，音频回调不做内存分配、设备打开或波形周期搜索。
- **保留解码后的细节**：媒体保留协商采样率，96 kHz LDAC 到 96 kHz 输出无需中间降为 48 kHz。内部保留浮点 PCM，必要重采样与微小时钟匹配合并处理，减少重复转换和量化。
- **连续性与音质一起验证**：每路使用独立时间线、自适应缓冲和有界缺口补偿，恢复窗口外保持解码样本；同率、正常增益的无缺口路径以逐样本相等检查。另用原生线程阻塞、抖动丢包、弱信号及频率参考验证边界。
- **隔离故障，保留可用路由**：实验 LDAC 在子进程内解码；端点切换采用准备与提交，过期操作不能覆盖新设备。异常按类型恢复，输入输出可热切换，挂断后继续媒体。

界面使用 C#/WPF；Python/Bumble 保留配对、服务发现、协议协商和应用控制。运行包不依赖 PyAV、NumPy 或 sounddevice。设计与可复现检查见 [音频核心](docs/AUDIO_CORE.md)、[保真契约](docs/AUDIO_FIDELITY.md) 和 [测试说明](docs/TESTING.md)。

## 运行环境与两种接收方式

支持目标为 **Windows 11/10，x64**。发布包自带 Python 控制后台和原生音频库；桌面使用上述系统自带的 .NET Framework 4.8 或 4.8.1，无需另装 Python 或现代 .NET Desktop Runtime。请使用完整包，不要只复制 EXE。

| 方式 | 用途 | 准备条件 |
|---|---|---|
| 声音和麦克风 · 专用适配器 | 双向语音、编码选择、多设备路由、AVRCP 控制 | 一个由 BlueHead 专用、可通过 WinUSB 访问的 USB HCI 蓝牙适配器 |
| 只收听媒体 · 共用 Windows 蓝牙 | 沿用 Windows 蓝牙驱动接收媒体 | 先在 Windows 中配对；编码与播放由系统管理，本程序不提供专用模式的语音和混音控制 |

专用接收会占用所选适配器。使用内置蓝牙时，该适配器原有的耳机、键鼠等连接会断开；也可使用单独的受支持 USB 蓝牙适配器。应用会备份恢复所需的信息，驱动更换和恢复需 Windows 管理员授权。停止接收后可恢复系统蓝牙。

不同适配器的启动方式不同。点击“开始接收”后，程序检查控制器状态和实际能力：已运行的固件直接验证使用；需要加载时使用本机已验证缓存，缺少文件则按提示在线获取或从原厂包提取。文件须通过签名目录与摘要校验。未适配的格式可使用一次原厂驱动引导，交接后重新确认专用接收；固件不能保留时恢复系统蓝牙。需要重启时明确提示 **“驱动恢复完成，请重启电脑”**，不会自动重启。见 [固件与启动说明](docs/FIRMWARE.md) 和 [控制器兼容范围](docs/COMPATIBILITY.md)。

## 第一次使用

1. 完整安装或解压同版本包，运行根目录的 `BlueHead.exe`。
2. 首页点击 **设置蓝牙接收**。需要麦克风时，选择“声音和麦克风 · 专用适配器”，按提示准备专用适配器。
3. 回到音频中心，点击 **开始接收**；在移动设备蓝牙设置中连接界面显示的名称，开启媒体与通话音频。
4. 播放媒体或开启支持蓝牙麦克风的应用。设备卡片的“播放 / 麦克风发送”调节该设备的增益；底部总控可调音量、静音及切换电脑声音设备。
5. AVRCP 可用时，曲名和三个媒体按钮显示在同一条紧凑信息行。设备没有提供有效元数据时显示“暂无曲目信息”；真实英文曲名保留原文。状态未知或操作不受支持时，相应按钮禁用。

首页同一行选择 **出声方式** 和 **媒体编码**。媒体编码只影响 A2DP，语音编码由双方自动协商。默认优先使用可用高清媒体编码；更换后按提示应用并重新连接。多设备同时播放高清音频若断续，可切换 AAC，必要时使用最高 192 kb/s，或改用单设备自动接替。

只收听媒体时可在 **蓝牙设置** 选择“只收听媒体 · 共用 Windows 蓝牙”，先在 Windows 配对，再回到首页开始接收。此方式保留 Windows 的其他蓝牙连接；编码、播放设备与音量由系统管理。首页、设置与帮助会显示当前方式，电脑麦克风与混音需切换专用接收。

选择方式不会直接更换驱动；准备不足时首页显示下一步。恢复当前适配器后自动切换系统共用，已恢复的驱动不能重复恢复；需要重启时明确提示。操作说明见 [界面指南](docs/UI_GUIDE.md)。

关闭窗口默认留在托盘；完全退出请右键托盘图标 → **退出**。恢复系统蓝牙前先停止接收，再到“蓝牙设置”。后台无法启动时，可单独运行 `BlueHead.exe --recovery`。

2.0 安装包要求新的空目录，默认使用 `%LocalAppData%\Programs\BlueHead-2`，目前不支持原地覆盖安装；安装版继续沿用原设置与配对目录。旧 1.x 安装版应在安装 2.0 前退出、备份并完成卸载，按提示处理系统蓝牙恢复。旧便携版可保留回退，在新目录使用 2.0；迁移便携版时只复制自己的 `data/`，不要覆盖新程序文件。不要在安装 2.0 后再运行旧安装版的卸载器。

## 协议与兼容边界

| 功能 | 实现及限制 |
|---|---|
| 媒体 | A2DP：SBC、AAC、aptX、aptX HD，以双方实际协商为准 |
| LDAC | 实验性、默认关闭；支持原生 44.1/48/88.2/96 kHz PCM，使用非 Sony 认证解码器 |
| 双向语音 | HFP：CVSD 8 kHz、mSBC 16 kHz、LC3-SWB 32 kHz；一次一个语音来源 |
| iPhone / iPad | 标准 AAC/SBC 媒体与 HFP 双向语音，配合 AVRCP 媒体信息和播放控制 |
| Android / HarmonyOS | 取决于系统、设备和应用实际开放的 profile、编码与麦克风路由，不以系统名称保证全部功能 |
| AVRCP | 曲目、播放状态、媒体控制与标准绝对音量；无有效媒体信息时显示“暂无曲目信息” |
| 电脑声音混合 | 进程排除采集要求 Windows build 20348+；Windows 10 22H2 不提供此项系统能力；windows 11 全系支持 |
| 录音 / 录像 | 应用必须支持并选择蓝牙麦克风，电脑不能强制指定移动端应用的输入源 |

**HFP LC3-SWB 不是完整 LE Audio。** 它要求双方支持相应 HFP 协商；蓝牙 5.3 或更高版本本身不能保证支持。当前不实现完整 LE Audio/BAP、Auracast、aptX Adaptive/Lossless 或同时多路语音桥接，也不声明获得 Apple 或 Bluetooth 产品认证。

AVRCP 的标准设备音量为 0%–100%，与本地 0%–200% 接收增益独立。卡片“详情”显示标准音量，远端调整后会影响实际接收声音，但不会把本地 175% 等增益改写为标准音量。

想保留 96 kHz LDAC 的原采样率，请将 Windows 播放端点的共享格式设为 96 kHz；端点为 48 kHz 时会做必要的抗混叠转换。软件 PCM／端点路径另验证到 384 kHz，实际来源和设备能力决定可用格式。音质契约见 [保真设计与验证](docs/AUDIO_FIDELITY.md)。

默认初始媒体缓冲 60 ms、语音 30 ms，运行中按抖动调整目标，并对短缺口补偿及恢复做平滑处理。这些缓冲值**不是端到端延迟实测值**。原生化旨在减少分配、调度和资源依赖；本版不承诺未经测量的延迟或 CPU 改善数值。没有声学回声消除，外放时实体麦克风仍会拾到声音，建议使用电脑耳机。

## 技术栈与源码

独立 `BlueHead.Runtime.exe` 统一持有 USB/HCI、固件初始化和音频图；WPF 与 Python 控制层通过私有接口发出会话和路由请求。新连接、重新协商及部分控制操作仍需要 Python/Bumble 运行。固件下载在独立进程中完成，不持有 USB/HCI，也不进入实时音频路径。

运行时不再依赖 PyAV、NumPy 或 sounddevice。原生组件包括音频专用 FFmpeg、单次异步采样率转换、兼容 SpeexDSP、miniaudio、liblc3，以及可独立替换的 SpanDSP PLC 动态库。界面仍为 C#/WPF，不裁剪 WPF/COM。

```text
bluehead/    蓝牙信令、会话；native_control/ 为控制接口
native/      audio/、codecs/、runtime/、transport/、controller/、firmware/、platform/、include/
desktop/     WPF 界面、系统接口、独立驱动工具与离线兼容检查
vendor/      固定版本上游源码、来源与校验信息
tools/       构建、诊断、验证与打包工具
tests/       协议、原生音频与生命周期回归
installer/   安装及卸载脚本
docs/        使用、架构、构建及验证说明
licenses/    第三方许可证
```

开发步骤见 [BUILDING](docs/BUILDING.md)，实现边界见 [ARCHITECTURE](docs/ARCHITECTURE.md)，音质约束与耳机参考设计见 [音频质量](docs/AUDIO_QUALITY.md)，具体测试记录见 [TESTING](docs/TESTING.md) 与同版本 `VALIDATION.md`。公开源码不包含个人配对、日志、驱动备份或构建缓存；安装包、便携包和源码归档放在 Releases。

## 数据与问题反馈

音频在本机实时处理，不录音、不上传声音。绿色版设置与配对在 `data/`，安装版在 `%LocalAppData%\BlueHead\data`；驱动恢复信息在 `%ProgramData%\BlueHead\Drivers`。升级请保留这些数据。

报告问题请附准确版本、Windows 版本、适配器、来源设备及复现步骤。诊断报告只导出匿名计数；请勿公开配对密钥、设备地址、完整恢复记录或含个人信息的原始日志。

项目默认采用 [Apache-2.0](LICENSE)，有单独许可声明的文件与第三方组件按其各自许可分发，见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。项目与 Microsoft、Google、Apple、Sony、Qualcomm 无隶属或认证关系。

## 源码与发布物

GitHub 源码只保留实现、测试、构建工具、使用说明、必要资源和固定依赖／许可证。
BlueHead 自有 EXE/DLL、FFmpeg 编译产物、测试编码器、完整安装包及本机日志／设置不进入源码树；
从干净检出运行 `tools/build_all_native.py` 可构建原生组件。保留的外部二进制为固定版本 libusb，附来源、许可证与 SHA-256 记录；厂商固件按需验证获取，源码与基础包均不附带镜像。
安装包、绿色版、源码归档和校验值单独通过 Releases 提供。开发与发布步骤见 [构建说明](docs/BUILDING.md)，
组件边界见 [架构说明](docs/ARCHITECTURE.md)，音频恢复规则见 [恢复设计](docs/RECOVERY.md)。
