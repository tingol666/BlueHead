# BlueHead

**把 Windows 电脑变成移动设备的蓝牙耳机。**

BlueHead is a free, open-source Bluetooth Classic audio receiver and hands-free endpoint for Windows. Listen to a mobile device on your computer and send the computer's microphone back over Bluetooth. No shared Wi-Fi or mobile companion app is required.

**2.0.1**：独立 C++ 实时音频后台、自适应缓冲和短缺口平滑恢复；保留 iPhone/iPad 标准音频、AVRCP 媒体控制和 HFP LC3-SWB。

[下载已发布稳定版](https://github.com/tingol666/BlueHead/releases/latest) · [使用指南](docs/UI_GUIDE.md) · [构建源码](docs/BUILDING.md) · [验证范围](docs/TESTING.md) · [报告问题](https://github.com/tingol666/BlueHead/issues)

![BlueHead 2.0.0 界面；设备、曲目信息和连接均为演示数据](docs/images/home-demo.png)

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

## 运行环境与两种接收方式

支持目标为 **Windows 11，x64**。发布包自带 Python 控制后台和原生音频库；桌面使用上述系统自带的 .NET Framework 4.8 或 4.8.1，无需另装 Python 或现代 .NET Desktop Runtime。请使用完整包，不要只复制 EXE。

| 方式 | 用途 | 准备条件 |
|---|---|---|
| 声音与麦克风 | 双向语音、编码选择、多设备路由、AVRCP 控制 | 一个由 BlueHead 专用、可通过 WinUSB 访问的 USB HCI 蓝牙适配器 |
| 仅接收声音 | 沿用 Windows 蓝牙驱动接收媒体 | 先在 Windows 中配对；编码与播放由系统管理，本程序不提供专用模式的语音和混音控制 |

专用接收会占用所选适配器。使用内置蓝牙时，该适配器原有的耳机、键鼠等连接会断开；也可使用单独的受支持 USB 蓝牙适配器。应用会备份恢复所需的信息，驱动更换和恢复需 Windows 管理员授权。停止接收后可恢复系统蓝牙。

## 第一次使用

1. 完整安装或解压同版本包，运行根目录的 `BlueHead.exe`。
2. 首页点击 **设置蓝牙接收**。需要麦克风时，选择“声音与麦克风”，按提示准备专用适配器。
3. 回到音频中心，点击 **开始接收**；在移动设备蓝牙设置中连接界面显示的名称，开启媒体与通话音频。
4. 播放媒体或开启支持蓝牙麦克风的应用。设备卡片的“播放 / 麦克风发送”调节该设备的增益；底部总控可调音量、静音及切换电脑声音设备。
5. AVRCP 可用时，曲名和三个媒体按钮显示在同一条紧凑信息行。设备没有提供有效元数据时显示“暂无曲目信息”；真实英文曲名保留原文。状态未知或操作不受支持时，相应按钮禁用。

首页同一行选择 **出声方式** 和 **媒体编码**。媒体编码只影响 A2DP，语音编码由双方自动协商。默认优先使用可用高清媒体编码；更换后按提示应用并重新连接。多设备同时播放高清音频若断续，可切换 AAC，必要时使用最高 192 kb/s，或改用单设备自动接替。

关闭窗口默认留在托盘；完全退出请右键托盘图标 → **退出**。恢复系统蓝牙前先停止接收，再到“蓝牙与驱动”。后台无法启动时，可单独运行 `BlueHead.exe --recovery`。

2.0 安装包要求新的空目录，默认使用 `%LocalAppData%\Programs\BlueHead-2`，目前不支持原地覆盖安装；安装版继续沿用原设置与配对目录。旧 1.x 安装版应在安装 2.0 前退出、备份并完成卸载，按提示处理系统蓝牙恢复。旧便携版可保留回退，在新目录使用 2.0；迁移便携版时只复制自己的 `data/`，不要覆盖新程序文件。不要在安装 2.0 后再运行旧安装版的卸载器。

## 协议与兼容边界

| 功能 | 实现及限制 |
|---|---|
| 媒体 | A2DP：SBC、AAC、aptX、aptX HD，以双方实际协商为准 |
| LDAC | 实验性、默认关闭，使用非 Sony 认证解码器 |
| 双向语音 | HFP：CVSD 8 kHz、mSBC 16 kHz、LC3-SWB 32 kHz；一次一个语音来源 |
| iPhone / iPad | 标准 AAC/SBC 媒体与 HFP 双向语音，配合 AVRCP 媒体信息和播放控制 |
| Android / HarmonyOS | 取决于系统、设备和应用实际开放的 profile、编码与麦克风路由，不以系统名称保证全部功能 |
| AVRCP | 曲目、播放状态、媒体控制与标准绝对音量；无有效媒体信息时显示“暂无曲目信息” |
| 电脑声音混合 | 进程排除采集要求 Windows build 20348+；Windows 10 22H2 不提供此项系统能力 |
| 录音 / 录像 | 应用必须支持并选择蓝牙麦克风，电脑不能强制指定移动端应用的输入源 |

**HFP LC3-SWB 不是完整 LE Audio。** 它要求双方支持相应 HFP 协商；蓝牙 5.3 或更高版本本身不能保证支持。当前不实现完整 LE Audio/BAP、Auracast、aptX Adaptive/Lossless 或同时多路语音桥接，也不声明获得 Apple 或 Bluetooth 产品认证。

AVRCP 的标准设备音量为 0%–100%，与本地 0%–200% 接收增益独立。卡片“详情”显示标准音量，远端调整后会影响实际接收声音，但不会把本地 175% 等增益改写为标准音量。

默认初始媒体缓冲 60 ms、语音 30 ms，运行中按抖动调整目标，并对短缺口补偿及恢复做平滑处理。这些缓冲值**不是端到端延迟实测值**。原生化旨在减少分配、调度和资源依赖；本版不承诺未经测量的延迟或 CPU 改善数值。没有声学回声消除，外放时实体麦克风仍会拾到声音，建议使用电脑耳机。

## 技术栈与源码

2.0.1 由独立 `BlueHead.Runtime.exe` 统一持有 USB/HCI、媒体解码、SCO 双向收发、PCM 桥接、混音、重采样和 WASAPI 音频流。已经建立的专用接收音频在 C/C++ 中持续运行，PCM 不经过 Python 或界面。Python 3.13 保留 Bumble 的配对、SDP、AVDTP/AVRCP、HFP 协商和应用控制；新连接、重新协商及部分控制操作仍需要它运行。

运行时不再依赖 PyAV、NumPy 或 sounddevice。原生组件包括音频专用 FFmpeg、SpeexDSP、miniaudio、liblc3，以及可独立替换的 SpanDSP PLC 动态库。界面仍为 C#/WPF，不裁剪 WPF/COM。

```text
bluehead/    Python 蓝牙信令、会话及原生控制接口
native/      C++ 运行时、USB/HCI、原生音频链路及连续性算法
desktop/     WPF 界面、系统接口、独立驱动工具与离线兼容检查
vendor/      固定版本上游源码、来源与校验信息
tools/       构建、诊断、验证与打包工具
tests/       协议、原生音频与生命周期回归
installer/   安装及卸载脚本
docs/        使用、架构、构建及验证说明
licenses/    第三方许可证
```

开发步骤见 [BUILDING](docs/BUILDING.md)，实现边界见 [ARCHITECTURE](docs/ARCHITECTURE.md)，音质约束与耳机参考设计见 [音频质量](docs/AUDIO_QUALITY_2.0.1.md)，具体测试记录见 [TESTING](docs/TESTING.md) 与同版本 `VALIDATION.md`。公开源码不包含个人配对、日志、驱动备份或构建缓存；安装包、便携包和源码归档放在 Releases。

## 数据与问题反馈

音频在本机实时处理，不录音、不上传声音。绿色版设置与配对在 `data/`，安装版在 `%LocalAppData%\BlueHead\data`；驱动恢复信息在 `%ProgramData%\BlueHead\Drivers`。升级请保留这些数据。

报告问题请附准确版本、Windows 版本、适配器、来源设备及复现步骤。诊断报告只导出匿名计数；请勿公开配对密钥、设备地址、完整恢复记录或含个人信息的原始日志。

项目默认采用 [Apache-2.0](LICENSE)，有单独许可声明的文件与第三方组件按其各自许可分发，见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。项目与 Microsoft、Google、Apple、Sony、Qualcomm 无隶属或认证关系。
