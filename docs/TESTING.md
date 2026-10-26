# 验证与验收

2.1.2 的分层与所有权见 [ARCHITECTURE.md](ARCHITECTURE.md)，音频时间线和音质检查见 [AUDIO_CORE.md](AUDIO_CORE.md)，异常分类及恢复见 [RECOVERY.md](RECOVERY.md)。每次构建的实际结果、包摘要及验收状态记录在同版本 `VALIDATION.md`。

默认自动化检查使用虚拟控制器、固定压缩样本和合成 PCM。真实计时回放、虚拟时钟长测、纯 UI 检查与实际移动设备验收分别记录。运行中的长测不计为通过；历史设备反馈也不扩写成本版验收。

## 不触硬件的检查

```powershell
.venv\Scripts\python.exe tools/sync_version.py --check
.venv\Scripts\python.exe tools/build_all_native.py
.venv\Scripts\python.exe -m pytest tests -q --tb=short
.venv\Scripts\python.exe -m ruff check bluehead tools tests launch.py engine_launch.py
work/publish-desktop/BlueHead.exe --self-test C:\Temp\BlueHead-ui
```

提交源码前运行 `tools/audit_sources.py --report work/source-audit.json`，同时验证 Git 索引与源码白名单一致。生成的 EXE/DLL 不属于 GitHub 源码，但完整包必须包含全部运行组件；这两个边界分别验证，不能通过精简源码把发布依赖遗漏。

协议测试应使用虚拟控制器、固定压缩样本和生成PCM。不可把打开真实播放、麦克风、USB或驱动绑定加入默认CI。

| 范围 | 主要检查 |
|---|---|
| 媒体 | RTP/分片边界、codec配置、坏包、暂停/恢复、挂断后媒体、旧代次隔离、AAC/SBC及高清编码处理 |
| 原生PCM | 任意块长、8–384kHz转换、有界队列、下混、非有限数、溢出后的旧语音丢弃、源增益与主增益 |
| 设备生命周期 | 新端点准备但不启动、失败回退、单渲染消费者、读取/统计与关闭同步、两个并发close、稳定endpoint身份 |
| 进程退出 | 系统打开卡住时daemon控制线程不阻止退出；正常退出清理自有后台及子进程 |
| HFP | CVSD/mSBC/LC3-SWB协商、H2/SCO分片、丢包/坏帧、双向字节处理、语音结束清理 |
| AVRCP | 命令生命周期、元数据边界、断开/迟到响应、播放状态、标准音量与本地增益隔离 |
| UI | 原紧凑两行、默认尺寸/DPI、实时状态、热切换确认、音量/静音、AVRCP22px行、未知状态禁用、正确LC3-SWB名称 |
| 包 | 版本一致性、DLL导入、无运行时PyAV/NumPy/sounddevice、无自包含coreCLR、必需恢复文件及许可证完整 |

`tests/test_audio_device.py`的native callback harness不初始化音频设备。它在内存中验证真实 DLL、原生 ASRC／兼容 Speex 转换、采集队列与释放顺序；卡住控制线程的检查在独立进程中退出。只读inventory可以枚举端点，但不等同声音测试。

Framework兼容层另有独立项目：

```powershell
dotnet restore desktop/BlueHead.FrameworkChecks/BlueHead.FrameworkChecks.csproj --locked-mode
dotnet run --project desktop/BlueHead.FrameworkChecks/BlueHead.FrameworkChecks.csproj -c Release --no-restore -- C:\Temp\BlueHead-framework
work/publish-helper/BlueHead.DriverHelper.exe --self-test
```

FrameworkChecks仅操作自己的临时文件和子进程：Windows参数引用、UTF-8、退出事件竞态、job子树清理、原恢复hash、原子替换和目录逃逸。helper self-test只读枚举适配器，不能证明驱动更换恢复成功。UI截图明确标注演示数据，不表示真实手机已连接。

## 扩展设备覆盖与后续硬件回归

以下为完整回归清单，不表示各项在本轮均已执行。已确认的范围以上表为准，其余须记录具体设备与结果。

1. 在完整待验收包中连续播放媒体，记录实际协商codec、电脑输出格式和设备，持续观察周期停顿、恢复和错误计数。
2. 使用Android及目标iPhone/iPad分别验证标准AAC/SBC媒体与HFP。接通双向语音、保持一段时间、挂断再继续媒体，至少重复两次。
3. 语音中切换真实电脑播放设备和麦克风，再回系统默认；插拔耳机检查默认跟随、列表与指定设备消失时回退，全程不要求蓝牙重连。
4. 检查每设备上下行0/100/200%、三路总控、静音/取消静音；AVRCP标准音量改变后，本地175%等增益仍保持独立。
5. 对支持AVRCP的播放器验证上/下曲、播放/暂停、曲名、艺术家、位置及断开清理。未知状态和拒绝操作不可产生猜测性toggle。
6. LC3-SWB只在明确支持的对端上验证实际协商32kHz及双向语音；不支持时应正常回退mSBC/CVSD，不因蓝牙5.3标识假定成功。
7. 两台设备同时播放：验证双AAC混音、单设备自动接替、手动选择、主动暂停不被误恢复及语音优先。高清并发不稳时明确选择较低码率重连。
8. 在支持进程排除采集的系统上，开启电脑声音混合，确认麦克风及其他应用可发送，BlueHead自己的输出不经数字回路返送；另留意实体麦克风的声学拾音。
9. 停止接收后验证恢复系统蓝牙、重新启用、取消UAC、错误提示与失败回退；随后验证安装、升级和卸载保护。驱动操作需使用者明确安排测试。
10. 托盘退出、停止/开始、设备断开/重连、后台失败和独立恢复窗口均须清楚可操作，无孤立后台或闪退。

记录机型、系统版本、适配器、连接方式、codec、来源内容和复现步骤。iOS通话成功不能自动推导相机/录音应用也会使用蓝牙麦克风。

## 性能与证据边界

缓冲60/30ms只是配置。native callback耗时、合成音频吞吐、CPU占用与蓝牙端到端延迟是不同指标。测端到端延迟须使用同一信号、明确设备与无线条件并报告测量方法；不能把微基准结果写成听感延迟。

无需为证明精简而删除必要组件。干净目标Windows应能离线启动完整包、运行独立恢复工具并加载所有codec；开发机上安装过SDK或旧版本程序不构成发布环境证明。

历史用户确认：1.2.2/1.2.3有双向语音、热切换、挂断媒体恢复与单设备接替记录；1.2.3最终包的周期停顿修复获确认。1.2.4只按当时要求完成自动化/离线验证。更详细历史见CHANGELOG与AUDIT。2.0.0历史结果单独记录在本页末尾，不能从历史记录扩大本版覆盖范围。

## 历史设备反馈：2.0.0

2026-10-26，用户在2.0.0构建`bf80c7`及其现有电脑/移动设备组合上确认：

| 场景 | 用户结果 |
|---|---|
| 持续媒体播放 | 正常 |
| 双向通话 | 正常 |
| 输入、输出设备热切换 | 正常 |
| 挂断后恢复媒体 | 正常 |
| AVRCP媒体控制与显示 | 正常；无元数据时出现远端英文占位文案 |

正式版随后在WPF呈现层将缺失元数据改为“暂无曲目信息”，保留真实英文曲名，并明确媒体编码与语音自动协商的区别。上述用户反馈对应`bf80c7`，不把它写成后续界面修正已重复进行的硬件测试；界面修正的自动回归结果见同版本VALIDATION.md。

本轮未提供可用于验收的iPhone/iPad或LC3-SWB对端，因此没有这些设备的实测结论；2.0.0安装/卸载、驱动更换和恢复也未完成本版实机验收。多设备压力、电脑声音混合、设备插拔、全部增益档位及端到端延迟等专项，不由上述五项基本反馈推导。旧版已有记录按原版本保留。

## 2.1.2 启动与驱动恢复验证

启动阶段针对 Intel 冷／热启动、固件完整性、USB 生命周期、可取消等待及驱动恢复状态机验证。随后的音质专项另覆盖原生采样率、单次转换、量化边界、事件窗口、时钟与原生所有权；两类证据分别记录。
`tests/test_intel_firmware.py` 使用离线 USB DLL 运行实际 C++ 所有者，未连接真实蓝牙；
`tests/test_startup.py` 验证固件加载中停止／取消与初始化错误传播；FrameworkChecks 区分已绑定、已就绪、需设备重启和需电脑重启。
完整重启和驱动切换不作为自动回归的隐含副作用。
