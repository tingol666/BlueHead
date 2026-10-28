# LE Audio / LC3 与 LHDC 5.0

2.1.5 在原有 A2DP / HFP 链路上增加 LE Audio 单播接收、麦克风发送、广播接收，以及 LHDC 5.0 媒体解码。它们复用同一个原生后台、声音设备、音量和混音系统，不另外打开 USB 或搬运 Python PCM。

## 连接方式与首次配对

首页“媒体编码”右侧齿轮 → **连接与编码 → 连接偏好**，提供三个选项。设置中的 LE Audio 选项与此同步；修改自动保存，下次停止并重新开始接收时生效。

| 选项 | 实际行为 | 适用情况 |
|---|---|---|
| 优先 LE Audio / LC3 | 同时提供经典蓝牙与 LE 服务；收到实际 LC3 音频后优先播放，未建立 LE 音频时保留经典媒体 | 已正确配对的双模设备、需要经典兼容 |
| 仅 LE Audio / LC3 | 关闭经典音频发现与连接入口，只提供 LE Audio；不自动回退经典蓝牙 | 首次 LE 配对、排查手机一直选择经典蓝牙 |
| 仅经典蓝牙 | 关闭 LE Audio 与广播，使用 A2DP / HFP | 不支持 LE Audio 的发送设备、日常经典音频 |

“优先”是电脑提供的偏好与本地实际流选择，不是对手机系统的强制命令。只隐藏经典搜索、短暂拒绝经典连接，再按定时器全局切回双模，会让一台设备的超时影响其它设备，且不能证明 LE 配对已成功。BlueHead 不通过这种方式打断正常音频。双模每条连接分别记录配对、加密、服务读取、LC3 配置、音频通道及真实音频到达；等待超时只解释缺少哪一步，不假装已使用 LC3。

若手机没有 LE Audio 选项，先选 **仅 LE Audio / LC3** 并停止、重新开始接收。在手机／平板“设置 → 蓝牙”中取消配对／忽略旧 BlueHead 条目，返回可用设备列表，重新找到同名设备并配对；打开详情中的“LE Audio／低功耗音频”（如提供），再播放音频。LC3 属于 LE Audio，不在手机的经典 A2DP 编码列表内。软件内 **LE 配对指南** 提供完整步骤。

Android AOSP 提供 **绕过蓝牙 LE 音频许可名单** 开发者选项，系统属性为 `persist.bluetooth.leaudio.bypass_allow_list`。可在设备开发者选项中搜索该名称，若可用则开启，重新打开手机蓝牙并重新配对。它与“停用 LE 音频硬件分流”不同，不能赋予手机或控制器缺少的能力。厂商系统可能隐藏或另行限制它；BlueHead 无法从 Windows 读取该开关，也不会声称已经绕过了手机策略。

AOSP 部分路径根据 DIS 中的型号字符串检查许可名单，单改蓝牙名称不等于改变型号。BlueHead 提供自己的标准设备信息、型号及软件版本；不以第三方厂牌、Fast Pair ID 或伪造的第二只耳机代替真实互通。仅 LE 模式也真实关闭了经典入口，广播中的能力与服务一致。改变发现方式后，手机的旧双模分类与服务缓存可能仍保留，因此需要上述重新配对步骤。

## 使用步骤

### LE Audio 媒体与电脑麦克风

1. 首页选择 **BlueHead 专用（进阶）**，按“蓝牙驱动设置”的提示准备所选适配器。
2. 点击首页媒体编码右侧的齿轮，在“连接与编码”选择 **优先 LE Audio / LC3** 或 **仅 LE Audio / LC3**；或在 **设置 → 高级音频设置** 的 LE Audio 开关旁选择。两处同步、自动保存。若已在接收，先回音频中心点 **停止接收**，再点 **开始接收**，然后重新连接发送设备。
3. 开关下方会分别显示这个适配器的媒体接收、麦克风发送、广播接收能力。只有实际检测支持的方向才开放服务；蓝牙 5.3、5.4 或更高的标签本身不能证明支持。
4. 在发送设备的蓝牙设置中连接电脑耳机。若系统提供“LE Audio／低功耗音频”选项，开启它；不同设备是否开放以及选项名称由其系统决定。
5. 播放媒体或在支持蓝牙麦克风的应用中选择蓝牙音频。设备卡片显示 **LE Audio / LC3**、实际采样率和实时码率。普通 A2DP 的媒体编码选项不会强行改变 LC3。

对端同时建立经典和 LE 连接时，界面按设备身份合并卡片。断开其中一条链路不会误删仍然有效的另一条。同一时间只向一个设备发送电脑麦克风；现有通话／语音保护继续覆盖新链路。

### 收听附近的音频广播

1. 完成上述 LE Audio 开启与开始接收；确认适配器的“广播接收”可用。
2. 在发送设备开启“音频分享”或“广播音频”。电脑打开左侧 **音频广播**，点击 **搜索附近广播**。
3. 点击某个广播右侧 **播放**。带密码广播会要求输入发送设备提供的密码；存在多个节目时可选择语言／节目。
4. 通过“广播音量”调节当前广播，音频中心的“设备声音”仍是播放总控。停止搜索只停止发现；**停止播放**才释放当前广播连接。

广播无需普通蓝牙配对。受信任的设备也可通过 BASS / PAST 传递连接信息；此时助手与电脑的控制连接需要加密。广播密码仅用于当前会话，不保存到设置、日志或诊断中。设备要使用电脑麦克风时，已播放的广播先停止；使用结束后可手动恢复广播。

### LHDC 5.0

点击首页媒体编码右侧的齿轮，在“连接与编码”的允许列表勾选 **LHDC 5.0**，再选为首选。点击 LHDC 5.0 旁的小齿轮，可设置允许的采样率与位深；通常保留“自动”即可。设置自动保存，修改后在发送设备的蓝牙设置中断开电脑耳机，再重新连接。手机／平板必须提供相应的 LHDC 5.0 A2DP 发送能力；实际格式由双方协商，设备卡片显示结果。它不等于 LE Audio，也不改变语音编码。

LHDC 的发送码率与质量档位由发送设备控制，电脑端不提供无法生效的远程档位设置。编码参数是全局偏好，不在已有连接中执行单设备热切换；经典编码的其它参数见 [编码与连接策略](CODEC_POLICY.md)。

支持 44.1/48/96/192 kHz、16/24-bit 立体声。电脑输出端点为同率时保留解码原采样率；端点为较低采样率时做必要的抗混叠 SRC。例如 192 kHz 来源并不能让只支持 48 kHz 的扬声器凭空获得 192 kHz 输出能力。

## 实现范围

| 部分 | 实现 |
|---|---|
| 基础音频服务 | PACS、ASCS、CAS，按实际能力公布；每条加密连接拥有自己的 ASE 状态 |
| LC3 接收 | 8/16/24/32/48 kHz；7.5/10 ms；20–400 字节／声道／帧；一或两帧块／SDU；单 CIS 双声道或双 CIS 分离立体声 |
| LC3 麦克风 | 原生单声道编码；格式和最大帧大小按控制器实际发送缓冲限制公布；沿用电脑麦克风及可选电脑声音混合 |
| 广播接收 | BASE / BIGInfo、PA / BIG / BIS、BASS / PAST、带密码广播、节目选择、单 BIS 或双 BIS 立体声 |
| 标准控制 | VCS 共享播放音量及静音、MICS 麦克风静音；加密、当前连接身份及有效控制计数检查 |
| LHDC 5.0 | A2DP 11 字节 vendor CIE、RTP 及 F/S/L 分片、独立原生解码进程、规范化浮点 PCM |

这是上述接收端与麦克风端点的范围，不包含广播发送、所有可选 GAF 服务或助听器功能。TMAS/TMAP 角色只按其完整依赖与适配器实际能力公布；不声明 Auracast 产品认证或其他 Bluetooth 认证。LC3plus/HR、LHDC 无损／AR／JAS／META 扩展未公布；不让对端协商软件不能处理的功能。标准资料见文末。

VCS 采用标准的共享播放状态，不是每台设备各存一个标准音量。它作为独立因子作用于 LE 音频和广播，保留本地每设备 0%–200% 增益。MICS 对端静音只改变发送因子；电脑端暂停发送优先，对端无法取消它。经典 AVRCP 标准音量继续沿用原有路径。

## 发现、身份与缓存

- 控制器初始化期间关闭经典发现与可连接入口。只有原生能力检测、完整 GATT 数据库、设备信息和连接处理均已就绪，才开放发现；LE 广播开始前，应用连接所有者已经登记。
- 双模使用同一公共身份，支持安全配对中的双向 CTKD 请求；LE 独立模式不发布经典音频能力。每个物理适配器使用随机生成并持久保存的本地 IRK，升级保留已有配对。
- 标准扩展广播保留完整 BAP/CAP 服务数据；可用服务同时出现在 UUID 列表中。控制器明确报告支持至少两个广播集合时，增加同地址、31 字节范围内的兼容发现广播与名称扫描响应。额外广播不替代必需的扩展广播；不占第二个 USB/HCI 所有者。
- 新设备使用普通音频通知。只有实际已配对设备的链路丢失才短暂使用 Targeted Announcement，随后恢复普通发现；任意历史配对记录不会让所有新设备一直处于重连通知方式。Targeted Announcement 与带指定地址的定向广播是不同概念。
- 经典与 LE 按配对身份合并设备卡片，处理随机地址解析与配对后身份变化。Service Changed 只恢复已配对、已订阅的客户端，确认后才更新数据库摘要；无确认不假装手机已刷新缓存。
- 一条立体声 CIS 与两条单声道 CIS 是音频拓扑；双耳独立地址与 CSIP 是设备集合拓扑。单台电脑公布左右声道能力，接收双方支持的音频拓扑，不虚构第二个物理设备或集合身份。

## 所有权、时间线与音质

`BlueHead.Runtime.exe` 是唯一 USB/HCI 所有者。标准 H4 bulk serialization 与明确探测到的厂商 ISO 传输才启用，不能根据 USB 型号或蓝牙版本猜测能力。CIS、BIS、SCO、父连接和本地广播终止在原生层先退役，然后把带连接代次的信令交给 Bumble。

Python 只配置服务、协商格式和操作带 lease/epoch 的控制接口。原生层完成 ISO 分片、信用、期限、LC3 解码／编码、麦克风采集、发送节拍和混音。它不会建立第二个 USB 所有者。解码子进程没有声卡和蓝牙句柄。

- 接收的两个声道共用一个时间线、一个混音来源和必要 SRC。各声道独立补偿缺失，单边丢包不改动另一边的有效样本。
- LC3 短缺口补偿最多延伸 40 ms，长缺口静音；解码／来源层是唯一缺口责任方，混音器不再次猜测丢失音频。
- 控制器时间戳与主机 QPC 以有界鲁棒拟合匹配。瞬时无线抖动不直接变成采样率变化；反馈不足时保持已有稳定估计。
- 协商呈现时延包含端点排队、回调和既有滤波开销，普通媒体缓冲不重复叠加。无法达到的预算在建立前明确拒绝，不无限扩大队列掩盖错误。
- 发送过期块直接退役，恢复后不追赶重放旧麦克风声音。已编码的实际负载字节用于实时码率，空闲后归零。
- LHDC 按每设备独立进程隔离上游共享状态；数学归一化和合成窗有独立参考，不靠主观音量补丁。完整有效帧才发布 PCM，错误帧不发布半个声道或未知尾部。

上述策略不降低用户所选的协商质量，不另加降噪门、重复 SRC 或整数 PCM 往返。实际丢失的信号无法凭空恢复；软件计时预算也不能当作实测声学延迟。

## 取消、断连与异常恢复

连接号会被控制器重用，因此仅比较数值句柄不够。原生层为连接建立分配单调代次，并在包到达时记录；准备／绑定同时核对父连接和 CIS/BIS 代次。旧准备、迟到通知、过期控件请求不能把旧缓冲绑定给新设备。

PA/BIG 建立、停止、改节目、错误密码、同步丢失分别处理。取消发送后仍由同一所有者收回应答和资源；第二次取消不会打断第一次的清理。错误密码允许重新输入，普通广播丢失有界重试；若终止结果不确定，先关闭原生声音，停止该控制器会话，不能假装释放成功再复用句柄。

HCI 发送与应答匹配由一个控制所有者负责；真正失去应答才终止，不把 Python 暂停误判为控制器失联。原生输出和输入仍走原来的准备／提交和热切换机制。需要原厂修复时在页面解释并等待点击，不自动请求管理员授权。

## 可复现的软件验证

公开构建与内置离线诊断见 [BUILDING](BUILDING.md)、[TESTING](TESTING.md)。维护回归测试和参考信号保留在开发环境，不是公开构建输入。

验证分层：

- 真实 Bumble 虚拟控制器、链路、SMP 加密、GATT 服务发现／通知、ASCS、CIG/CIS 与原生桥接；不是只调用内部状态对象。
- BASS 密码、PAST、节目变化、坏广播、资源限额、反复取消、父连接丢失、连接号复用、广播与麦克风互斥。
- LC3 独立通道参考、真实浮点编解码和 ISO 分片／信用；Python 阻塞、原生传输／工作线程阻塞及丢包抖动注入。
- 原生虚拟时钟长测，含主机／控制器时钟漂移、时间戳绕回、反馈缺失。这是加速模型测试，不冒充小时级实机播放。
- LHDC 独立 AOSP 编码信号、合成窗／变换参考、静音与弱信号、立体声与高采样率、坏码流、真实 AVDTP/L2CAP 协商、多个解码进程及崩溃隔离。
- 同版本原组件的经典媒体、HFP、挂断恢复、热切换、电脑混音、保真与界面回归。

虚拟链路不模拟完整射频、控制器固件与手机策略，不能替代实际移动设备的媒体、双向语音和广播互通。最终结果应按 [TESTING](TESTING.md) 的设备矩阵独立记录，未运行的项目不能写成通过。

## 标准与上游

- Bluetooth SIG：[BAP](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BAP_v1.0/out/en/index-en.html)、[ASCS](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/23166-ASCS-html5/out/en/index-en.html)、[CAP](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/CAP_v1.0.1/out/en/index-en.html)、[BASS](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BASS_v1.0.1/out/en/index-en.html)。
- 标准音量／静音：[VCS](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/77401-VCS-html5/out/en/index-en.html)、[MICS](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/22708-MICS-html5/out/en/index-en.html)。
- 信令：[Google Bumble](https://github.com/google/bumble)，固定修订见锁文件；LC3：[Google liblc3](https://github.com/google/liblc3)，固定来源见 `vendor/liblc3`。
- LHDC：[独立解码器](https://github.com/WillyBilly06/LHDC-V5-Decoder/tree/cf857ce1a1429965e161e54adbc3f293fd9ce273)，原始源码摘要、许可声明和构建适配记录见 `vendor/lhdc5-decoder`；原生适配器见 `native/codecs/lhdc5_stream.c`、`lhdc5_media.c`。

## 发现与手机互通参考

- [Bluetooth SIG：耳机发现、配对与连接指南](https://www.bluetooth.com/wp-content/uploads/2025/04/DiscoveryPairingAndConnectionForEarbuds_INFO_v1.pdf)：单设备与独立双耳、同地址广播、CTKD 的不同责任。
- [Android LE Audio 设备建议](https://developer.android.com/develop/connectivity/bluetooth/ble-audio/overview)：双向 CTKD、音频上下文、对端媒体／通话控制服务。
- [AOSP PhonePolicy](https://android.googlesource.com/platform/packages/modules/Bluetooth/+/b760df280c1b0313bf1c1360f4f7538a1568d2f4/android/app/src/com/android/bluetooth/btservice/PhonePolicy.java)、[许可名单开关](https://android.googlesource.com/platform/packages/apps/Settings/+/7c598253ff60f06f8e6fe046f18fd88e9daa72d3/src/com/android/settings/development/BluetoothLeAudioAllowListPreferenceController.java)、[DIS 型号读取](https://android.googlesource.com/platform/packages/modules/Bluetooth/+/e1acd8fbb8fb76907cb26130105e6942081e7946/system/btif/src/btif_dm.cc)：平台策略证据，不代表每个厂商系统实现完全相同。
- [Zephyr BAP Unicast Server](https://github.com/zephyrproject-rtos/zephyr/blob/main/samples/bluetooth/audio/bap_unicast_server/src/main.c)、[TMAP Peripheral](https://github.com/zephyrproject-rtos/zephyr/blob/main/samples/bluetooth/audio/tmap_peripheral/src/main.c)：公开服务、UUID、广播与连接流程参考。
- [Sony 官方 LE Audio 配对步骤](https://helpguide.sony.net/mdr/2984/v1/en/contents/TP1001872731.html)：切换连接方式后重新配对的用户流程参考。
