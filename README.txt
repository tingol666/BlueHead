BlueHead 2.1.6 / Windows 10 and 11 x64
https://github.com/tingol666/BlueHead

把 Windows 电脑作为蓝牙耳机：收听设备媒体，将电脑麦克风发回设备。
无需同一 Wi-Fi 或手机客户端。请使用完整程序目录，不要单独复制 EXE。

首次使用
1. 运行 BlueHead.exe，在首页选择“蓝牙使用方式”。
2. Windows 共用（基础）：保留系统蓝牙。先在电脑 Windows 蓝牙设置中配对设备，回 BlueHead 点“开始接收”，再在设备上播放媒体。
3. BlueHead 专用（进阶）：电脑麦克风、多设备、高级编码、LE Audio 和广播。在“蓝牙驱动设置”按从上到下的说明准备，点右下方“备份并启用专用蓝牙”；完成后回首页手动点“开始接收”，再在设备蓝牙列表连接电脑耳机名称。
4. 专用会断开所选适配器原有系统蓝牙连接；没有其它系统适配器时 Windows 蓝牙开关会消失。需要恢复时在同一页点“恢复 Windows 蓝牙”。

声音与协议
每设备播放／麦克风发送以及底部总控支持 0%–200%、静音和输入输出热切换。
点击“电脑声音”选择采集来源。默认“全部应用声音（排除 BlueHead）”；指定播放设备须与 BlueHead 的播放设备分开，避免返送。采集不可用时不影响麦克风。
电脑声音混合要求 Windows build 20348+，通常为 Windows 11；当前无声学回声消除，建议戴耳机。
A2DP：SBC/AAC/aptX/aptX HD、实验 LDAC、LHDC 5.0。
首次安装默认启用全部可用编码，可在“连接与编码”中调整；升级保留已有的选择。传输持续不稳定时，按首页提示改用较低码率编码（如 AAC）并重新连接。
HFP：CVSD/mSBC/LC3-SWB。iPhone/iPad 标准 AAC/SBC、HFP、AVRCP。
LE Audio：LC3 单播媒体和麦克风发送、音频广播接收、BASS/PAST、VCS/MICS。
手机没有 LE Audio 选项时，打开“连接与编码”内的“LE 配对指南”：选择“仅 LE Audio / LC3”，停止并重新开始接收，再在手机上忽略旧 BlueHead 条目后重新配对。指南也说明 Android 许可名单开关。
首页媒体编码右侧的齿轮打开“连接与编码”，选择 LE Audio 偏好、允许的经典编码与首选。每个经典编码旁的小齿轮可设置其采样率及可用的位深、码率或质量上限，自动保存。实际格式仍由双方能力协商，设备卡片显示结果。
修改经典编码设置后，在发送设备蓝牙设置中断开电脑耳机再重新连接。修改 LE 连接偏好后，先在 BlueHead 点“停止接收”→“开始接收”，再重新连接发送设备。首页会显示待重连提示，不执行单设备编码热切换。
语音和 LE Audio 编码自动协商；蓝牙版本号不能保证协议能力，应用必须支持并选择蓝牙麦克风。
一次向一个设备发送麦克风。不提供广播发送、LC3plus、aptX Adaptive/Lossless、LHDC 可选无损/AR/JAS/META 扩展或全部可选 GAF 应用角色。

关闭窗口默认留在托盘，单击图标显示主界面；完全退出请右键托盘→退出。
启动异常请到“蓝牙驱动设置”按状态操作，需要时点右下方“修复专用蓝牙”。
不会后台突然请求管理员授权。若提示驱动恢复完成但需要重启，请重启 Windows 后再用。

技术与数据
C/C++ 原生后台持有 USB/HCI，持续音频传输、编解码、浮点混音、SRC 与 WASAPI 不经过 Python PCM。
Python/Bumble 保留配对、协商与控制；桌面为 C#/WPF、.NET Framework 4.8。
无需另装 Python 或现代 .NET Desktop Runtime。运行不依赖 PyAV/NumPy/sounddevice。
音频不录音、不上传。广播密码不持久保存。通用包不附带厂商固件，匹配文件按需校验获取或从原厂包提取，缓存后可离线使用。

升级与许可
安装器要求新的空目录，目前不支持覆盖安装。升级前从托盘退出并备份自己的数据。
绿色版数据在 data/，安装版在 %LocalAppData%\BlueHead\data。
驱动恢复记录在 %ProgramData%\BlueHead\Drivers，固件缓存为 %LocalAppData%\BlueHead\Firmware。
保留原驱动备份，不混用不同版本程序文件。反馈请勿公开配对密钥、真实地址和个人日志。
功能、构建和文档：https://github.com/tingol666/BlueHead
同版本实际软件检查见 VALIDATION.md。许可见 LICENSE、THIRD_PARTY_NOTICES.txt。
