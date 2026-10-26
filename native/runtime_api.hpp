/* Native ABI references outlive all USB/audio workers. Apache-2.0. */
#pragma once
#include "runtime_common.hpp"
extern "C" {
#include "audio_device.h"
#include "media_engine.h"
#include "voice_engine.h"
}
namespace bh {
// clang-format off
#define AUDIO_APIS(X) \
  X(bh_media_log_quiet, void, ()) \
  X(bh_mix_create, void *, ()) \
  X(bh_mix_free, void, (void *)) \
  X( bh_mix_add, int, (void *, uint32_t, uint32_t)) \
  X(bh_mix_remove, void, (void *, int)) \
  X(bh_mix_clear, void, (void *, int)) \
  X(bh_mix_gain, void, (void *, int, float)) \
  X( bh_mix_gate, void, (void *, int, int)) \
  X(bh_mix_call, void, (void *, int)) \
  X(bh_mix_master, void, (void *, float)) \
  X(bh_mix_clock_config, int, (void *, float, float)) \
  X( bh_mix_render, int, (void *, float *, uint32_t, float)) \
  X(bh_mix_write, void, (void *, int, const float *, uint32_t)) \
  X( bh_mix_write_s16_mono, void, (void *, int, const int16_t *, uint32_t)) \
  X(bh_mix_value, uint64_t, (void *, int, int)) \
  X(bh_media_create, BhMedia *, (int, int, int, int)) \
  X( bh_media_destroy, void, (BhMedia *)) \
  X(bh_media_restart, int, (BhMedia *)) \
  X(bh_media_feed, int, (BhMedia *, const uint8_t *, uint32_t)) \
  X(bh_media_drain_mixer, uint32_t, (BhMedia *, void *, int)) \
  X(bh_media_discard, void, (BhMedia *)) \
  X(bh_media_value, uint64_t, (BhMedia *, int)) \
  X( bh_up_create, void *, ()) \
  X(bh_up_free, void, (void *)) \
  X(bh_up_clear, void, (void *, int)) \
  X( bh_up_rate, int, (void *, uint32_t)) \
  X(bh_up_adaptive, int, (void *, int)) \
  X( bh_up_gain, void, (void *, float, float, float, int)) \
  X(bh_up_write, void, (void *, int, const uint8_t *, uint32_t)) \
  X(bh_up_read, int, (void *, uint8_t *, uint32_t)) \
  X(bh_up_value, uint64_t, (void *, int, int)) \
  X(bh_capture_create, void *, (uint32_t, uint32_t, uint32_t)) \
  X(bh_capture_free, void, (void *)) \
  X(bh_capture_push, int, (void *, const float *, uint32_t)) \
  X(bh_capture_read, int, (void *, int16_t *, uint32_t)) \
  X(bh_mix_master_value, float, (void *))
#define DEVICE_APIS(X) \
  X(bh_device_offline_process,int,(BhAudioDevice*,const float*,float*,uint32_t)) \
  X(bh_device_offline,BhAudioDevice*,(BhDeviceContext*,uint32_t,void*,uint32_t,uint32_t,uint32_t,int*)) \
  X(bh_device_context_create, BhDeviceContext *, (const wchar_t *, int *)) \
  X(bh_device_context_close, int, (BhDeviceContext *)) \
  X(bh_device_inventory, int, (BhDeviceContext *, BhEndpoint *, uint32_t, uint32_t *)) \
  X(bh_device_open_output, BhAudioDevice *, (BhDeviceContext *, void *, const wchar_t *, int *)) \
  X(bh_device_open_capture, BhAudioDevice *, (BhDeviceContext *, const wchar_t *, uint32_t, int *)) \
  X(bh_device_start, int, (BhAudioDevice *)) \
  X(bh_device_stop, int, (BhAudioDevice *)) \
  X(bh_device_close, int, (BhAudioDevice *)) \
  X(bh_device_active, int, (BhAudioDevice *)) \
  X( bh_device_stats, int, (BhAudioDevice *, BhDeviceStats *)) \
  X(bh_device_read, int, (BhAudioDevice *, int16_t *, uint32_t)) \
  X(bh_device_ready_event, void, (BhAudioDevice *, void *))
#define VOICE_APIS(X) \
  X(bh_voice_create, bh_voice *, (uint32_t)) \
  X(bh_voice_destroy, void, (bh_voice *)) \
  X( bh_voice_encode, int, (bh_voice *, const int16_t *, uint32_t, uint8_t *, uint32_t)) \
  X(bh_voice_push_rx, int, (bh_voice *, const uint8_t *, uint32_t, uint32_t)) \
  X(bh_voice_read_pcm, uint32_t, (bh_voice *, int16_t *, uint32_t)) \
  X(bh_voice_conceal, int, (bh_voice *, uint32_t)) \
  X(bh_voice_get_stats, int, (const bh_voice *, bh_voice_stats *, uint32_t))
#define LOOP_APIS(X) \
  X(bh_loop_open, void *, (DWORD, DWORD *)) \
  X(bh_loop_read, int, (void *, float *, UINT)) \
  X(bh_loop_error, DWORD, (void *)) \
  X(bh_loop_close, int, (void *)) \
  X(bh_loop_ready_event, void, (void *, void *))
#define CONTINUITY_APIS(X) \
  X(bh_mix_continuity,int,(void*,int,int,uint32_t,uint32_t)) \
  X(bh_up_continuity,int,(void*,int)) \
  X(bh_media_continuity,int,(BhMedia*,int))
// clang-format on
struct Api {
  Module audio, device, voice, loop;
#define DECLARE(name, ret, args)                                                                   \
  using name##_type = ret(*) args;                                                                 \
  name##_type name = nullptr;
  AUDIO_APIS(DECLARE)
  CONTINUITY_APIS(DECLARE)
  DEVICE_APIS(DECLARE)
  VOICE_APIS(DECLARE)
  LOOP_APIS(DECLARE)
#undef DECLARE
  explicit Api(const fs::path &p)
      : audio(p / L"bluehead_audio.dll"), device(p / L"bluehead_device.dll"),
        voice(p / L"bluehead_voice.dll"), loop(p / L"bluehead_loopback.dll") {
#define LOAD(name, ret, args) name = audio.get<name##_type>(#name);
    AUDIO_APIS(LOAD)
    CONTINUITY_APIS(LOAD)
#undef LOAD
#define LOAD(name, ret, args) name = device.get<name##_type>(#name);
    DEVICE_APIS(LOAD)
#undef LOAD
#define LOAD(name, ret, args) name = voice.get<name##_type>(#name);
    VOICE_APIS(LOAD)
#undef LOAD
#define LOAD(name, ret, args) name = loop.get<name##_type>(#name);
    LOOP_APIS(LOAD)
#undef LOAD
  }
};
} // namespace bh
