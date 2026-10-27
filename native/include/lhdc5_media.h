/* Isolated native RTP/LHDC V5/float boundary. SPDX-License-Identifier: Apache-2.0. */
#pragma once
#include "lhdc5_stream.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef struct BhLhdc5Media BhLhdc5Media;
BH_LHDC_API BhLhdc5Media *bh_lhdc5_media_create(int rate, int channels, int depth);
BH_LHDC_API void bh_lhdc5_media_destroy(BhLhdc5Media *media);
BH_LHDC_API int bh_lhdc5_media_feed(BhLhdc5Media *media, const uint8_t *packet, uint32_t bytes);
BH_LHDC_API uint32_t bh_lhdc5_media_read(BhLhdc5Media *media, float *pcm, uint32_t frames);
BH_LHDC_API uint64_t bh_lhdc5_media_value(BhLhdc5Media *media, int field);
#ifdef __cplusplus
}
#endif
