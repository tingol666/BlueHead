/* BlueHead native voice framing and codec engine. SPDX-License-Identifier: MIT */
#ifndef BLUEHEAD_VOICE_ENGINE_H
#define BLUEHEAD_VOICE_ENGINE_H
#include <stdint.h>
#ifdef _WIN32
#define BH_VOICE_API __declspec(dllexport)
#else
#define BH_VOICE_API __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif

/* ABI v1: all PCM is mono, signed 16-bit, 48000 Hz. Samples != bytes.
 * One context belongs to ONE serialized worker. No function calls Python or
 * creates threads. The caller must stop/join users before reset/destroy; calls
 * on separate contexts may run concurrently. No call after destroy is valid.
 * Creation allocates framing/PCM/LC3/SRC state. libavcodec mSBC may allocate
 * internally on the serialized worker; this API is not an OS audio callback.
 * HCI SCO packet boundaries are deliberately independent of codec boundaries.
 */
typedef struct bh_voice bh_voice;
enum { BH_VOICE_ABI = 1, BH_VOICE_CVSD = 1, BH_VOICE_MSBC = 2, BH_VOICE_LC3_SWB = 3 };
enum { BH_VOICE_FRAME_SAMPLES = 360, BH_VOICE_QUEUE_FRAMES = 32 };
typedef struct bh_voice_stats {
  uint32_t abi_version, codec_id;
  uint64_t rx_packets, rx_bytes, rx_frames, tx_frames;
  uint64_t plc_frames, missing_frames, corrupt_frames, duplicate_frames;
  uint64_t resync_bytes, queue_overruns, discarded_samples;
  uint32_t queued_samples, high_water_samples;
} bh_voice_stats;

BH_VOICE_API uint32_t bh_voice_abi_version(void);
BH_VOICE_API uint32_t bh_voice_codec_available(uint32_t codec_id);
BH_VOICE_API bh_voice *bh_voice_create(uint32_t codec_id);
BH_VOICE_API void bh_voice_destroy(bh_voice *voice);
BH_VOICE_API int bh_voice_reset(bh_voice *voice);
/* Fixed encoding frame: 360 PCM samples. Returns bytes written, or <0.
 * Capacity errors do not consume sequence/codec state. CVSD here means HCI
 * 8k S16 PCM (controller does CVSD coding), not a software CVSD bitstream. */
BH_VOICE_API int bh_voice_encode(bh_voice *voice, const int16_t *pcm, uint32_t samples,
                                 uint8_t *wire, uint32_t capacity);
/* Additive float bridge: normalized mono 48k PCM, 360 frames. No intermediate
 * S16 quantization for LC3; CVSD/mSBC quantize only at their codec boundary. */
BH_VOICE_API int bh_voice_encode_f32(bh_voice *voice, const float *pcm, uint32_t samples,
                                     uint8_t *wire, uint32_t capacity);
/* Returns complete received/concealed frames queued, or <0 for bad arguments.
 * packet_status is HCI packet status 0..3; nonzero marks those bytes damaged.
 * Explicit data-less losses should use conceal; their duration cannot be
 * inferred from a zero-byte HCI event. At most 4096 input bytes per call. */
BH_VOICE_API int bh_voice_push_rx(bh_voice *voice, const uint8_t *wire, uint32_t length,
                                  uint32_t packet_status);
BH_VOICE_API uint32_t bh_voice_read_pcm(bh_voice *voice, int16_t *pcm, uint32_t capacity);
BH_VOICE_API uint32_t bh_voice_read_f32(bh_voice *voice, float *pcm, uint32_t capacity);
BH_VOICE_API int bh_voice_conceal(bh_voice *voice, uint32_t frames);
BH_VOICE_API int bh_voice_get_stats(const bh_voice *voice, bh_voice_stats *stats,
                                    uint32_t stats_size);
#ifdef __cplusplus
}
#endif
#endif
