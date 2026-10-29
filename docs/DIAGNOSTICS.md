# 诊断

## 软件内导出

打开“帮助与诊断”，点击“刷新状态”，再“导出诊断报告”。报告包含版本、原生音频计数和状态，不包含录音或广播密码。反馈前仍请检查是否包含希望保密的信息；不要公开配对文件、完整驱动恢复记录、真实设备地址或未经检查的原始日志。

界面监督后台失败时会在应用数据目录保存有界的 `engine-supervisor.log`，包含后台代次、退出码和最后一段错误信息；可能有本机路径或调用栈。默认匿名数值导出不复制这段原始错误。

## 显式底层抓包

设置环境变量 `BLUEHEAD_HCI_TRACE_DIR` 后启动程序，会在所选本地目录记录现有 HCI 所有者的信令及状态，用于深入排查；默认不启用，也不包含在普通诊断导出中。抓包可能包含真实设备地址、配对密钥及经典媒体负载，必须按敏感资料保管，不要上传到公开 Issue。原生实时 ISO/SCO 不为此绕经 Python。诊断结束后移除该环境变量，后续启动不再抓包。

## 不连接硬件的内置检查

先创建报告目录。源码按 [BUILDING](BUILDING.md) 构建全部原生组件后：

```powershell
New-Item -ItemType Directory -Path work -Force
.venv\Scripts\python.exe engine_launch.py --offline-audio-check work\offline-native.json
```

完整发布包根目录：

```powershell
app\engine\BlueHead.Engine.exe --offline-audio-check offline-native.json
```

离线诊断采用产品自带的合成压缩向量与原生内存端点，不打开 USB、真实播放设备或麦克风。结果仅证明列出的软件路径完成，不等于某个适配器、移动设备或应用已经互通。

源码构建的桌面可用 `BlueHead.exe --self-test 报告目录` 检查界面状态和渲染；它使用演示连接，不改变驱动。这些内置诊断所需源码与向量是产品的一部分，随公开树保留。维护用回归测试、硬件探针、基准临时文件和原始报告不随公开源码或运行包分发。

## 真正会访问系统的检查

`app\engine\BlueHead.Engine.exe --system-audio-check 报告路径` 会初始化真实 Windows 电脑声音采集。它不播放测试音、不录音到文件，也不能证明完整混音或无线通话质量。只在需要排查这一项时手动执行。

切换／恢复驱动或原厂初始化请在“蓝牙驱动设置”按状态操作；故障不会自动请求管理员授权。若恢复完成后提示需要重启，请重启 Windows，不要反复切换。
