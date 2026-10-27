/* BlueHead native LC3 stream boundary. SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include <stdint.h>
#ifdef _WIN32
#define BH_LC3_API __declspec(dllexport)
#else
#define BH_LC3_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
typedef struct bh_lc3_stream bh_lc3_stream;
/* A single worker owns a stream. Buffers are interleaved, normalized float PCM.
 * Only LE Audio LC3 configurations are accepted; LC3plus is not substituted.
 * No allocation, resampling, device I/O, scheduling or PLC outside liblc3 occurs
 * in encode/decode. SDU assembly and the playout timeline belong to the caller. */
BH_LC3_API uint32_t bh_lc3_abi(void);
BH_LC3_API bh_lc3_stream *bh_lc3_create(uint32_t rate, uint32_t duration_us,
                                      uint32_t channels, uint32_t octets_per_frame);
enum { BH_LC3_ENCODE = 1, BH_LC3_DECODE = 2 };
BH_LC3_API bh_lc3_stream *bh_lc3_create_ex(uint32_t rate, uint32_t duration_us,
                                         uint32_t channels, uint32_t octets_per_frame,
                                         uint32_t directions);
BH_LC3_API uint32_t bh_lc3_workspace_bytes(const bh_lc3_stream *stream);
BH_LC3_API int bh_lc3_reset_rx(bh_lc3_stream *stream);
BH_LC3_API int bh_lc3_reset_tx(bh_lc3_stream *stream);
BH_LC3_API void bh_lc3_destroy(bh_lc3_stream *stream);
BH_LC3_API int bh_lc3_reset(bh_lc3_stream *stream);
BH_LC3_API uint32_t bh_lc3_samples(const bh_lc3_stream *stream);
BH_LC3_API uint32_t bh_lc3_frame_bytes(const bh_lc3_stream *stream);
BH_LC3_API int bh_lc3_encode(bh_lc3_stream *stream, const float *pcm,
                            uint32_t samples_per_channel, uint8_t *encoded, uint32_t capacity);
/* NULL plus zero bytes requests exactly one lost frame block. Return 1 means
 * liblc3 concealed at least one channel, 0 means all channels decoded normally.
 * Invalid envelope/capacity returns -1 without advancing any codec state. */
BH_LC3_API int bh_lc3_decode(bh_lc3_stream *stream, const uint8_t *encoded,
                            uint32_t bytes, float *pcm, uint32_t sample_capacity);
/* Split-CIS stereo keeps one timeline and independent per-channel histories.
 * Valid bits decode data, conceal bits request liblc3 PLC, remaining channels
 * produce exact zero without changing their decoder state. Masks must be
 * disjoint. The caller alone bounds PLC and resets a silent channel on resume.
 * Return the bitmask of channels for which liblc3 performed concealment. */
BH_LC3_API int bh_lc3_decode_mask(bh_lc3_stream *stream,const uint8_t *encoded,
                                  uint32_t bytes,uint32_t valid_mask,uint32_t conceal_mask,
                                  float *pcm,uint32_t sample_capacity);
BH_LC3_API int bh_lc3_reset_rx_channel(bh_lc3_stream *stream,uint32_t channel);
#ifdef __cplusplus
}
#endif
