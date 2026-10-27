/* BlueHead isolated LHDC V5 boundary. SPDX-License-Identifier: Apache-2.0. */
#pragma once
#include <stdint.h>
#ifdef _WIN32
#define BH_LHDC_API __declspec(dllexport)
#else
#define BH_LHDC_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
typedef struct bh_lhdc5_stream bh_lhdc5_stream;
/* Upstream math has process-wide state. One stream per codec process is
 * enforced. Never load this decoder into the shared USB/audio runtime. */
BH_LHDC_API uint32_t bh_lhdc5_abi(void);
BH_LHDC_API bh_lhdc5_stream *bh_lhdc5_create(uint32_t rate, uint32_t depth);
BH_LHDC_API void bh_lhdc5_destroy(bh_lhdc5_stream *stream);
BH_LHDC_API void bh_lhdc5_reset(bh_lhdc5_stream *stream);
BH_LHDC_API uint32_t bh_lhdc5_samples(const bh_lhdc5_stream *stream);
/* One complete encoded frame, including its two-byte header. Returns frames
 * per channel, or a negative error. Invalid output is never published. PCM is
 * normalized interleaved float, without SRC, extra integer quantization, level
 * trims or saturation. A single caller owns the stream. */
BH_LHDC_API int bh_lhdc5_decode(bh_lhdc5_stream *stream, const uint8_t *packet,
                                uint32_t bytes, float *pcm, uint32_t capacity);
BH_LHDC_API uint64_t bh_lhdc5_value(const bh_lhdc5_stream *stream, uint32_t field);
#ifdef __cplusplus
}
#endif
