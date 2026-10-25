"""Exercise real local audio from the receiver thread; no radio, no recordings."""
import asyncio
import faulthandler
import json
import logging
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from bumble import hci, hfp
    from bumble.device import Device
    from bumble.host import Host
    import sounddevice as sd
    from bluehead.audio import AudioOutput, Mixer
    from bluehead.calls import CallManager, Msbc

    report = {"passed": False, "scope": "receiver-thread local duplex, silent output, no radio or saved audio"}
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("work/duplex-thread-probe.json")
    system_mix = "--system-mix" in sys.argv
    if system_mix:
        report["scope"] = "Local duplex with external quiet test tone; actual mic, capture, resampling, gain, mSBC and hot switching; no radio or saved audio"
    logging.basicConfig(level=logging.INFO)

    async def exercise():
        mixer = Mixer()
        device = Device(host=Host())
        uplink_chunks = []
        uplink_decoder = Msbc()
        def collect_uplink(handle, data):
            if system_mix:
                uplink_chunks.extend(uplink_decoder.decode(data))
        device.host.send_sco_sdu = collect_uplink
        manager = CallManager(device, mixer, lambda kind, **data: logging.info("%s %s", kind, data))
        report["host_apis"] = [{"name": a["name"], "default_output_device": a["default_output_device"]} for a in sd.query_hostapis()]
        output = AudioOutput(mixer)
        report["opened_output"] = {"index": output.stream.device, "rate": output.stream.samplerate}
        output.start()
        pump = None
        try:
            link = SimpleNamespace(handle=71, air_mode=hci.CodecID.TRANSPARENT, once=lambda *args: None)
            manager.on_link("local-check", SimpleNamespace(active_codec=hfp.AudioCodec.MSBC), link)
            async with asyncio.timeout(4):
                while manager.microphone is None:
                    await asyncio.sleep(.02)
            silence = Msbc().encode(bytes(240))
            async def receive_silence():
                while True:
                    packet = hci.HCI_SynchronousDataPacket(connection_handle=71, packet_status=0,
                                                          data_total_length=60, data=silence)
                    link.sink(packet)
                    await asyncio.sleep(.0075)
            pump = asyncio.create_task(receive_silence())
            if system_mix:
                import numpy as np
                await manager.set_system_audio(True)
                manager.router.microphone_gain = 0  # Isolate the computer source for measurement.
                async def measure():
                    await asyncio.sleep(.2)  # Drain any previous encoder/filter state.
                    uplink_chunks.clear()
                    await asyncio.sleep(.4)
                    audio = np.concatenate(uplink_chunks)
                    # RMS is invariant to FFT-bin scalloping and the small
                    # clock corrections of a live SCO capture/render pipeline.
                    return float(np.sqrt(2 * np.mean(audio.astype(np.float64) ** 2)))
                normal = await measure()
                manager.router.system_gain = 0
                silent = await measure()
                manager.router.system_gain = 2
                doubled = await measure()
                manager.router.device_gains['local-check'] = .5
                per_device = await measure()
                assert normal > .002 and silent < normal * .03, (normal, silent)
                assert 1.8 < doubled / normal < 2.2, (normal, doubled)
                assert .85 < per_device / normal < 1.15, (normal, per_device)
                report['system_uplink'] = {'normal': normal, 'zero_gain': silent, 'double_gain': doubled,
                                           'per_device_half_after_double': per_device,
                                           'capture_frames': manager.system_capture.frames}
                manager.router.microphone_gain = 1
                manager.router.system_gain = 1
                manager.router.device_gains.clear()
            devices, apis = sd.query_devices(), sd.query_hostapis()
            wasapi = next(i for i, api in enumerate(apis) if "WASAPI" in api["name"])
            inputs = [i for i, d in enumerate(devices) if d["hostapi"] == wasapi and d["max_input_channels"] >= 1
                      and any(name in d["name"] for name in ("麦克风", "Microphone"))
                      and not any(name in d["name"] for name in ("Voicemeeter", "VB-", "CABLE"))][:2]
            outputs = [i for i, d in enumerate(devices) if d["hostapi"] == wasapi and d["max_output_channels"] >= 2
                       and any(name in d["name"] for name in ("Realtek", "JM98MAX", "USB Audio"))][:2]
            report["call_input_switches"] = []
            for selected in inputs + [None]:
                before_rx = manager.statistics["local-check"]["rx_packets"]
                before = manager.microphone.frames
                await manager.set_input_device(selected)
                await asyncio.sleep(.15)
                count = manager.microphone.frames - before
                assert count > 0 and manager.statistics["local-check"]["rx_packets"] > before_rx
                report["call_input_switches"].append({"device": selected, "capture_frames": count})
            report["call_output_switches"] = []
            for selected in outputs + [None]:
                before_rx = manager.statistics["local-check"]["rx_packets"]
                await asyncio.to_thread(output.switch_device, selected)
                await asyncio.sleep(.05)
                assert output.stream.active and manager.statistics["local-check"]["rx_packets"] > before_rx
                report["call_output_switches"].append({"device": selected, "active": bool(output.stream.active)})
            from bluehead.audio_runtime import refresh_devices, streams_unhealthy, on_audio_thread
            await asyncio.to_thread(on_audio_thread(lambda: output.stream.abort()))
            assert streams_unhealthy()
            report["inactive_stream_detected"] = True
            report["runtime_refreshes"] = []
            for _ in range(3):
                before = manager.microphone.frames
                mapped, errors = await asyncio.to_thread(refresh_devices, {"output": output.device, "input": manager.input_device})
                manager.input_device = mapped["input"]
                await asyncio.sleep(.15)
                assert not errors and output.stream.active and manager.microphone.frames > before
                assert not streams_unhealthy()
                assert manager.links["local-check"] is link
                report["runtime_refreshes"].append({"active": True, "voice_link_preserved": True,
                                                   "capture_frames": manager.microphone.frames - before})
            if system_mix:
                assert manager.system_capture and manager.system_capture.frames > 0
                await manager.set_system_audio(False)
                assert manager.system_capture is None and manager.links['local-check'] is link
                report['system_toggle_keeps_voice_link'] = True
            report["microphone_frames"] = manager.microphone.frames
            report["voice_counters"] = manager.statistics["local-check"]
            assert report["microphone_frames"] > 0
            assert report["voice_counters"]["decode_errors"] == 0
            report["passed"] = True
        finally:
            if pump:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            manager.close()
            # Newer managers provide asynchronous capture cleanup.
            if hasattr(manager, "wait_closed"):
                await manager.wait_closed()
            await asyncio.to_thread(output.close)

    def run():
        try:
            asyncio.run(exercise())
        except Exception:
            import traceback
            report["error"] = traceback.format_exc()

    with open("work/duplex-thread-probe-stacks.txt", "w", encoding="utf-8") as stack:
        faulthandler.dump_traceback_later(12, file=stack)
        thread = threading.Thread(target=run, daemon=True, name="receiver-probe")
        thread.start()
        thread.join(15)
        faulthandler.cancel_dump_traceback_later()
        report["thread_finished"] = not thread.is_alive()
        report["passed"] = report["passed"] and report["thread_finished"]
        path.write_text(json.dumps(report, indent=2), "utf-8")
        print(json.dumps(report, indent=2), flush=True)
        if thread.is_alive():
            os._exit(2)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
