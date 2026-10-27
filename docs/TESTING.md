# 验证与验收

音频时间线和保真契约见 [AUDIO_CORE.md](AUDIO_CORE.md) 与 [AUDIO_FIDELITY.md](AUDIO_FIDELITY.md)，异常恢复见 [RECOVERY.md](RECOVERY.md)，控制器初始化与证据范围见 [COMPATIBILITY.md](COMPATIBILITY.md)。每次正式包的实际结果、校验摘要和验证范围记录在同版本 VALIDATION.md。

默认测试不枚举或操作真实蓝牙、不录音、不修改驱动。虚拟控制器、固定压缩样本、合成 PCM、实时回放、虚拟时钟长测、界面检查和真实设备验收分别记录；未完成的检查不算通过。

## 自动化入口

基础源码不再包含可执行的厂商固件镜像。涉及原厂完整载荷的引导模型使用忽略目录 `work/firmware-fixtures` 中经过签名目录校验的资源；缺少时明确跳过，不自行联网。需要运行这些检查时，构建原生后台后执行：

    bluehead/assets/BlueHead.Runtime.exe --firmware ensure --firmware-profile intel-tlv-0041 --firmware-cache work/firmware-fixtures --allow-network

也可用 `BLUEHEAD_TEST_FIRMWARE` 指向已经准备好的内容缓存。普通构建、签名校验及错误路径测试不要求下载厂商镜像。

    .venv/Scripts/python.exe tools/sync_version.py --check
    .venv/Scripts/python.exe tools/build_all_native.py
    .venv/Scripts/python.exe -m pytest tests -q --tb=short --basetemp=work/test-run
    .venv/Scripts/python.exe -m ruff check bluehead tools tests launch.py engine_launch.py
    .venv/Scripts/python.exe tools/codec_signal_audit.py --report work/codec-signals.json
    dotnet build desktop/BlueHead.FrameworkChecks/BlueHead.FrameworkChecks.csproj -c Release
    desktop/BlueHead.FrameworkChecks/bin/Release/net48/BlueHead.FrameworkChecks.exe work/framework-checks
    desktop/BlueHead.Desktop/bin/Release/net48/BlueHead.exe --self-test work/ui-checks

为并行测试选择不同的 basetemp，避免复用被其他进程占用的临时目录。提交前运行 tools/audit_sources.py，核对 Git 索引与源码白名单。发布包另运行 tools/audit_package.py；不能用源码精简来省略运行依赖。

编码边界检查使用固定 AOSP LDAC 编码器、独立 FFmpeg 解码参考和实际原生媒体／语音 DLL，覆盖数字静音、音乐尾部、空声道、弱信号、精细量化、重置及丢包恢复。`codec_signal_audit.py` 只保存数值与 DLL 摘要；静音 RMS 为零时 `rms_dbfs` 用 JSON null 表示负无穷。可通过 `--baseline-ldac` 显式指定旧开发 DLL 做对照，无需将旧二进制加入源码或运行包。模拟 CVSD 仅覆盖控制器交出的 PCM 边界，不能代替无线控制器内的 CVSD 实现检查。

| 范围 | 主要验证 |
| --- | --- |
| 媒体与协议 | RTP、分片、codec 配置、坏包、暂停与挂断后恢复、代次隔离、AVRCP、HFP 编码协商 |
| 原生音频 | 8–384 kHz、原采样率、单次 SRC、量化与限幅、短缺口、时钟漂移、抖动丢包注入及信号对照 |
| 实时所有权 | USB/HCI 单所有者、原生线程／Python 阻塞、回调最坏耗时、单一渲染消费者、旧会话清理 |
| 端点与混音 | 输入输出热切换、部分初始化失败、超时、默认设备跟随、多设备接替、电脑声音数字回路排除 |
| 控制器身份 | 同型号重排、明确物理选择、旧 VID/PID 选择歧义、跨进程租约、只读探测 |
| 固件 | CNG 目录签名、摘要与长度、缓存损坏、完整镜像／配置验证、未知芯片／板卡拒绝 |
| 厂商初始化 | Intel 冷热启动；四种 Realtek 固件格式对照；原生下载、错误应答与超时；标准 HCI 能力与地址 |
| 原厂包 | ZIP、CAB、MSI 静态流、嵌入 CAB；目录越界、链接、重复文件、扩展大小限制；不执行安装动作 |
| 驱动与引导 | 原驱动排名、INF 内容确认、有限交接、固件不能保留、重启中止、失败回退与受保护阶段记录 |
| 界面与包 | 页面模式一致、启动补救、已恢复与待重启操作、仅媒体能力、原生依赖位置、版本和源码边界 |

Realtek 对照向量来自可信目录固定修订的未修改文件，预期载荷由固定 Bumble 解析器独立生成，许可随源码保留。标准 HCI 和交接模型验证软件决策，不等同于每种物理硬件的无线电测试。

## 设备兼容验收

使用自己的目标硬件组合依次检查：

1. 重启后开始接收，连接移动设备并持续播放；记录实际媒体编码、采样率与输出端点。
2. 开启双向语音，在过程中切换输入输出，挂断后继续媒体；检查支持蓝牙麦克风的录音／录像应用。
3. 双设备同时播放与单设备自动接替；检查每设备和总增益、静音、电脑声音混合及无数字返送。
4. 核对 AVRCP 控制、元数据和标准音量；不同平台按实际开放的 profile 与应用路由验收。
5. 停止接收，恢复系统蓝牙，再启用专用接收；记录驱动状态、初始化和需要重启时的提示。
6. 增加同型号适配器、改变枚举顺序及端口，确认只选择明确的物理目标。

不把一次正常播放推广成全部协议、所有芯片或完整重启场景均通过。兼容矩阵按证据扩展。
