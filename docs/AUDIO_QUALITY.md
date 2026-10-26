# 音质与连续性约束

音频设计同时约束音质、延迟和实时预算。不能用降低媒体编码、强制降低对端采样率、无限加缓冲或持续重复音频来掩盖故障。实际构建与验收结果记录在 Release 的 `VALIDATION.md`；本文件描述设计和可复现检查，不替代实机听感验收。

## 从耳机参考设计借鉴什么

参考 Nordic nRF Audio 耳机固件的同步模块，以及 BlueKitchen BTstack 的 A2DP Sink 示例：

- 把接收、解码、播放时钟分开。包到达间隔不是 DAC 播放时钟，不能把每次到达抖动都当作采样率改变。
- 把启动同步与持续漂移校正分开。Nordic 在启动校准后进行小幅音频时钟调整；Windows 应用不能随意调整耳机 DAC 的晶振，因此 BlueHead 使用平滑的软件时钟匹配。
- 缓冲区有明确的上下界，音频数据有独立的生产者、消费者和生命周期。网络恢复后不能重放失效连接的旧 PCM。
- 确实缺失的音频才进入 PLC。支持的语音编码保留其原生丢帧处理；通用媒体补偿必须有限，并对非周期内容保守处理。

这些是公开的芯片/协议栈参考设计，不能据此断言所有商业耳机的私有固件都采用同一种算法。Nordic 的硬件 PLL、LE Audio SDU 时间戳和 TWS 呈现同步也不能直接套到 Windows A2DP。这里只借鉴职责、状态和验证方法，没有移植 Nordic 或 BTstack 的受限代码。

## 处理契约

| 环节 | 正常输入 | 缺口或切换 | 防止的退化 |
| --- | --- | --- | --- |
| USB/HCI | 一个物理所有者，原生异步接收 | 有界队列、取消后等待完成、会话代次校验 | Python 停顿拖住收发；旧分片进入新连接 |
| 解码时间线 | 保持 RTP 序号、时间戳、编码帧顺序 | 保留完整缺帧时长，波形预测预算与时间长度分开；超过 200 ms 的跳转另作流中断 | 把 21.33 ms 包压成 20 ms 累计耗尽缓冲；混淆解码失败、无线丢包和 PCM 缺口 |
| 时钟匹配 | 连续 SRC，相同左右声道相位 | 平滑追踪目标，保留校正余量 | 正常收包也复制/跳过波形产生时间接缝 |
| 自适应缓冲 | 快增慢减，按音频帧计时 | 真正饥饿后才重缓冲 | 小回调导致缓冲过快缩小；重复整块补零 |
| 媒体缺口 | 不修改正常信号 | 两声道分别验证去直流后的周期相关性；低置信度快速淡出；最长有限补偿与恢复合并 | 把直流、噪声或另一声道的瞬态重复成嗡声 |
| 增益与通话压低 | 5 ms 连续渐变，维持左右比例 | 真正的 0/静音立即生效，清理相应待输出音频 | 回调边界的音量阶跃；取消静音重放旧音频 |
| 防过载 | 不超过数字满幅时增益为 1 | 约 1.5ms 前瞻、双声道联动、连续攻击/释放 | 整块峰值归一化导致响度跳变和块长依赖 |
| 采样率与输出 | 保留来源原生 PCM，按端点率合并 SRC 与时钟匹配 | 不可变转换方案准备后提交，保护旧消费者 | 先降成 48k 再升回；重复转换／过早量化 |

48k 是统一时间基准，不限制 PCM 格式。媒体与语音保留原生采样率，转换到实际端点时将必要 SRC 与漂移匹配合并；同率且无需时钟修正时绕过滤波。双精度 Kaiser 相位值／导数、Hermite 插值与 SSE2 共享双声道运算兼顾精度和有界开销。滤波器及状态在控制侧准备，兼容入口的 SpeexDSP 状态继续保留。详细信号基线及实现结果见 [AUDIO_FIDELITY](AUDIO_FIDELITY.md)。

前瞻按实际采样率换算为约 1.5 ms（48k 为 72 帧，96k 为 144 帧），单独在 `quality` 诊断项报告；不能把它称作蓝牙端到端延迟。限幅仅在混合/增益产生过载时介入，释放时间 70 ms；正常近满幅的 0.995 瞬态也应透明。过载不可能同时保持原幅度又不削顶，测试应检查连续性与声道比例，不能承诺 200% 下任何输入都无失真。

每个来源拥有自己的前瞻音频，暂停、清空或复用槽位时只清除该来源。删除一路不能丢掉另一路的待输出声音。Windows 已提交的端点缓冲不在这个软件契约之内。

## 用什么验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_audio_quality.py tests/test_continuity.py tests/test_realtime.py tests/test_output_formats.py tests/test_callback_timing.py tests/test_audio_counters.py -q
.venv\Scripts\python.exe tools/audio_quality_benchmark.py --report work/audio-quality-cost.json
```

`test_audio_quality.py` 使用真正的生产 C 音频库：

- 多频混合、近满幅瞬态以及过载输入，比较固定/不规则回调的输出；不能只用单频正弦判断音质。
- 在固定有限延迟后，正常 unity 信号与输入逐样本比较，检查瞬态、完整频谱和立体声比例。
- 单设备、总增益、通话压低的阶跃变化；真正静音；单来源清空与槽位复用隔离。
- 带直流偏置的随机立体声，防止误触发周期复制。
- 55/95/150/440 Hz 与 1/7.5/16 kHz、各 16 种起始相位的短缺口，检查低频音高和左右反相信号。周期判断同时约束原始窗口差值与去直流相关性，避免低频的短平移被误认为完整周期。
- 44.1/48/96/192 kHz 的真实输出格式转换块长一致性，以及转换中的静音尾部。

`test_continuity.py` 另用连续时间斜坡叠加音调，在无丢包、仅到达抖动条件下检测倒退/跃进的时间接缝。长期时钟、RTP 回绕、独立麦克风/电脑声音、Python GIL 和整个控制进程暂停测试仍保留。最终二进制必须重新进行全量检查和长测，旧构建的通过结果不能代替新构建。

它还检查 AAC/aptX HD 单包及 SBC 连续包丢失超过 20 ms 时，输出仍保留完整缺失时长。反复 AAC 丢包、-500 ppm 和到达抖动的 2 分钟虚拟传输不得累计丢弃或欠载。预测淡出后剩余的缺帧时间允许静音，不能因此删除时间或无限复制波形。压缩自检短样本的循环会重复编码器起始过渡，故逐编码合成回放区分源本身近零段和传输缺口。

## 可定位的诊断

原生状态同时包含压缩包丢失、解码失败、PCM 缺口、补偿、重缓冲、时钟校正、过载限幅和输出回调计数。`prefill_frames` 也可能在暂停/空闲来源等待输入时增长，不能把它直接当作听到静音的时长。回调执行耗时与回调到达间隔分别统计；到达间隔异常不是 WASAPI 欠载的直接证据。

仅在诊断启动时设置 `BLUEHEAD_AUDIO_COUNTER_LOG=1`，Python 控制进程才将数值状态写入数据目录的 `audio-counters.log`，最多每 2 秒一次，单文件 2 MB 加一个轮转文件。日志不包含 PCM、压缩音频、设备名称/地址、配对密钥或曲目信息；磁盘写入不会进入原生收发/音频线程。写入失败会停用此日志。发布前应恢复默认关闭。

## 参考来源

- [Nordic nRF Audio 固件架构与耳机同步模块](https://nrfconnectdocs.nordicsemi.com/ncs/latest/nrf/applications/nrf_audio/doc/firmware_architecture.html)
- [Nordic audio_datapath.c](https://github.com/nrfconnect/sdk-nrf/blob/main/applications/nrf_audio/src/audio/audio_datapath.c)
- [BTstack A2DP Sink 示例](https://github.com/bluekitchen/btstack/blob/master/example/a2dp_sink_demo.c)
- [WebRTC AGC2 limiter](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/agc2/limiter.cc)：参考持续的增益状态与攻击/释放职责，不复制其实现。
- [miniaudio](https://github.com/mackron/miniaudio)：参考按采样帧平滑增益；已有依赖固定在本仓库 `vendor/miniaudio`。
- [PipeWire 蓝牙时钟控制](https://github.com/PipeWire/pipewire/blob/master/spa/plugins/bluez5/rate-control.h)：已固定版本与 MIT 许可，见 `vendor/pipewire-rate-control`。
