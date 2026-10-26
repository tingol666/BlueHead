# 2.0.1 音频连续性验证

2.0.1 把持续音频路径放到独立 C++ 进程。验证分为算法、进程隔离、实际 USB 接口及移动设备验收；每次构建的实际结果以同版本 `VALIDATION.md` 为准。

音质、前瞻限幅、增益平滑和耳机参考设计的取舍见 [音质与连续性约束](AUDIO_QUALITY_2.0.1.md)。正常收包不再复制/跳过周期波形追赶缓冲，缺口补偿只在实际短缺时介入。

音质、前瞻限幅、增益平滑和耳机参考设计的取舍见 [音质与连续性约束](AUDIO_QUALITY_2.0.1.md)。正常收包不再复制/跳过周期波形追赶缓冲，缺口补偿只在实际短缺时介入。

## 可复现的离线检查

先按 [BUILDING](BUILDING.md) 构建原生组件，再运行：

```powershell
.venv\Scripts\python.exe -m pytest tests -q --tb=short
.venv\Scripts\python.exe -m ruff check bluehead tools tests launch.py engine_launch.py
```

`test_native_runtime.py` 启动真实原生 EXE，验证 HCI 分片、旧会话隔离、原生采集事件、超时取消、暂停与 PCM 提交、静音状态及启动退出。它还让 Python 持有 GIL，并暂停独立 Python 控制进程的全部线程，检查原生媒体和双向语音继续运行。无对端下行音频的录音场景单独验证麦克风发送。

`test_native_guard.py` 把原生多设备损坏流保护与原 Python 策略逐步比较，再用两路真实解码回放检查：损坏的竞争来源被限制，剩下唯一来源时恢复。

`test_continuity.py` 使用实际 C 音频库，检查短缺口、长断流、恢复接缝、真实解码与补偿帧的分别计数、RTP 来源重启，以及带抖动和时钟偏差的音调、幅度及立体声同步。同时覆盖 RTP 时间戳与序号回绕。历史 30 分钟上行、10 分钟下行和格式转换检查继续保留，增强上行也单独检查 30 分钟时钟偏差。

## 实际计时回放与丢包对照

```powershell
.venv\Scripts\python.exe tools/native_soak.py --seconds 15 --codec sbc --drop-every 31 --jitter-ms 3 --legacy-continuity --report work/legacy.json
.venv\Scripts\python.exe tools/native_soak.py --seconds 15 --codec sbc --drop-every 31 --jitter-ms 3 --report work/adaptive.json
.venv\Scripts\python.exe tools/native_soak.py --seconds 20 --codec aac --voice-codec 3 --gil-stall-ms 3000 --report work/duplex.json
```

旧策略开关只用于同一原生回放框架内的比较，不冒充旧版完整程序。固定压缩样本经过真实 HCI 重组、解码、PCM 队列及渲染；声音端点为内存中的合成端点，不打开蓝牙、扬声器或麦克风。

30 分钟后台压力测示例（无需实机播放或人工值守）：

```powershell
.venv\Scripts\python.exe tools/native_soak.py --seconds 1800 --codec sbc --drop-every 31 --jitter-ms 3 --clock-ppm -500 --gil-stall-ms 2000 --snapshot work/soak/assets --report work/soak/result.json
```

`--snapshot` 保存本次原生输入的独立副本；报告记录逐文件 SHA256、计数、内存及 CPU 时间。测试临时阻止自动睡眠，结束时释放；创建报告同名的 `.stop` 文件可中止。中止或仍在运行的报告不能计为通过。

静音段指标从第 1 秒后开始，阈值为双声道绝对值之和小于 `1e-8`。真实信号的零点、保守衰减和启动预缓冲需区别理解。回放耗时、CPU 和软件缓冲不是蓝牙端到端延迟。

## 26 小时虚拟时钟

```powershell
.venv\Scripts\python.exe tools/build_clock_stress.py
work/clock-stress/clock-stress.exe bluehead/assets work/clock-26h.json 93600
```

两个独立的原生 PCM 混音器分别模拟 ±500 ppm、±3 ms 到达抖动。测试实际渲染 26 小时 PCM，跨越 32 位帧数，检查队列边界、短缺口、丢弃、漂移、1 kHz 音调和立体声同步。它使用显式虚拟到达时间，不将测试机的调度停顿当作无线抖动；正式播放仍使用真实 QPC。

长测曾暴露：校正上限与源时钟偏差同为 ±500 ppm 时，抖动会让控制器缺少恢复余量，队列可能逐渐耗尽。增强媒体路径现保留 ±1000 ppm 校正余量；源偏差测试仍为 ±500 ppm，缓冲上限和通过门槛没有放宽。

## 实机验收

原生 USB 打开关闭、只读 HCI 命令及静音共享输出检查，只证明相应接口工作。发布前还需移动设备完成连续媒体、双向语音、输入输出热切换、挂断恢复、双 AAC 与单设备接替，并检查电脑声音混合无数字返送。记录实际机型、系统、适配器、编码和结果。

## 参考实现

- [PipeWire Bluetooth rate control](https://github.com/PipeWire/pipewire/blob/master/spa/plugins/bluez5/rate-control.h)：独立音频时钟匹配；固定来源与 MIT 许可保存在 `vendor/pipewire-rate-control`。
- [WebRTC NetEq](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_coding/neteq/g3doc/index.md)：参考其自适应缓冲、缺口补偿和恢复的职责分工，本项目未移植完整 NetEq。
- [libusb 异步 I/O](https://libusb.sourceforge.io/api-1.0/group__libusb__asyncio.html)：单事件所有者、异步传输及取消完成后释放。
- [Microsoft MMCSS](https://learn.microsoft.com/en-us/windows/win32/procthread/multimedia-class-scheduler-service)：原生音频线程调度。
