# Intel 蓝牙启动与系统恢复

## 使用方式

AX211 在专用 WinUSB 驱动下重启或断电后，控制器可能回到 RAM 固件尚未加载的引导模式。
USB 设备可枚举、PnP 状态正常，不代表蓝牙协议已经可以运行。

2.1.2 完整包包含匹配的 Intel 签名镜像。用户只需点击“开始接收”：

1. 原生 USB 所有者读取引导状态及硬件标识。
2. 已在运行模式时直接继续；引导模式时验证镜像、分片下载并等待控制器签名验证及启动通知。
3. 确认运行模式、应用设备配置后，蓝牙协议栈才开始正常初始化。

此操作加载的是易失 RAM 固件，不升级硬件闪存，不替换 Windows 驱动，也不联网下载。
加载期间可以停止接收；进程会先取消和回收全部 USB 请求，再释放适配器。

“恢复系统蓝牙”会先尝试初始化唯一 AX211，然后释放原生 USB 所有权并绑定备份的原厂驱动。
恢复检查使用适配器的实际设备实例及 Windows 蓝牙无线电接口，不会拿另一块蓝牙的状态充数。
如果原厂驱动需要更长时间，会先等待，必要时仅重新启动这个适配器一次。
**驱动已恢复但蓝牙仍未完成初始化时，提示“驱动恢复完成，请重启电脑”。**
驱动未绑定成功、设备断开与上述结果分开报告，原恢复备份一直保留。

## 当前范围

内置镜像为 `BT_Solar_REL82200_23.50.26292.82200`，用于匹配的 AX211 Solar 硬件。
文件基名根据控制器返回的 CNVI/CNVR 生成，不仅根据 `8087:0033` 猜测。
支持目录中明确列出的 `0040/1040` 与 `0041/1050/4150` 组合；别名来自上游 WHENCE，
`0040-0041` 和 `1040-0041` 两份原始镜像也逐字节相同，因此仅保存一份。

Intel AX210/BE200 的运行状态也可识别；没有对应的内置冷启动镜像时明确提示未适配，
不会把 AX211 镜像写给其它型号。原厂驱动已经初始化好的运行固件不会被强制替换。
多块相同 AX211 同时存在时，驱动恢复前不猜测 SetupAPI 与 libusb 的枚举顺序；
交回原厂驱动自行初始化，再按实际无线电状态决定成功或重启提示。

## 实现边界

- `native/transport/intel_firmware.hpp`：有界 TLV／镜像／DDC 校验、逐命令确认、签名和启动状态。
- `native/transport/runtime_usb.hpp`：同一个 libusb 句柄、USB 事件线程及跨进程 HCI 租约。
  引导阶段 bulk IN 使用事件格式，Secure Send 走 bulk OUT；进入运行模式后恢复 ACL 格式。
- `native/runtime/runtime_core.hpp`：异步开始和状态轮询，不把完整固件下载塞进同步 RPC 超时。
- `desktop/BlueHead.Platform`：原厂驱动备份／绑定、按实例匹配无线电、恢复就绪判定和重启提示。
- 驱动操作期间禁止新的普通接收器取得 HCI 所有权；固件准备工具也必须取得同一份独占租约。

固件阶段与 Bumble 的运行期命令记账不重叠。完整镜像及配置在第一次下载前验证；
镜像大小、SHA-256、签名容器和命令边界不符就停止。控制器仍自行执行签名检查。
每个命令、上传总时长、启动通知与 USB 关闭均有限等待，不无限重试或反复重置硬件。

## 来源与许可

- [Bumble Intel 驱动与协议实现](https://github.com/google/bumble/blob/e1fe43c9b44be0d974a6fd533ac6d494fc50510a/bumble/drivers/intel.py)，Apache-2.0；本实现参考其初始化序列和 Intel 安全引导布局。
- [linux-firmware 固定修订](https://kernel.googlesource.com/pub/scm/linux/kernel/git/firmware/linux-firmware/+/98b55c84e8da5c09dd9b8d21f5667c63db5993fa/)，保存未修改的 Intel 固件与 DDC。
- `vendor/intel-firmware/origin.json` 记录每个文件的 SHA-256、上游 Git blob、别名与版本。
- `licenses/intel-bluetooth-firmware/LICENCE.ibt_firmware` 保留 Intel 的二进制再分发许可及免责声明；固件不受本项目 Apache-2.0 许可替代。
- [Microsoft 蓝牙无线电接口](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/guid-bthport-device-interface)与 [BluetoothGetRadioInfo](https://learn.microsoft.com/en-us/windows/win32/api/bluetoothapis/nf-bluetoothapis-bluetoothgetradioinfo)用于恢复后的就绪检查。

## 验证

默认离线测试通过模拟 USB DLL 运行生产版 C++ 所有者，使用真实固定镜像规划下载，
覆盖冷／热启动、bulk 分片、命令／签名拒绝、DDC 失败、超时、取消、缺失与损坏文件。
驱动就绪策略另有纯状态测试，不为运行测试而切换真实驱动或重启电脑。

本机 AX211 已通过受控的“引导模式→签名固件加载→运行模式”检查，约 1.7 秒完成；
重新打开约 0.17 秒，跳过重复下载，标准 HCI 版本和缓冲查询均成功。测试没有重启电脑、
更换驱动、连接移动设备或播放／录音。实际电脑重启与恢复原厂驱动流程未执行，
不把控制器级验证写成所有 Windows 启动场景均已验收。
