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
/* The next feed consumes this context, in source-rate sample frames. Zero
 * forbids extra gap PCM; never calling the setter retains the legacy budget. */
BH_LHDC_API void bh_lhdc5_media_gap_context(BhLhdc5Media *media, uint32_t free_source_frames,
                                         uint32_t remaining_source_frames);
BH_LHDC_API uint32_t bh_lhdc5_media_read_fenced(BhLhdc5Media *media, float *pcm,
                                            uint8_t *fence, uint32_t frames);
/* Query before read. The last actual drained frame clears drain confidence;
 * a pending genuine recovery fence survives a gap-only drain. */
BH_LHDC_API int bh_lhdc5_media_drain_confidence(BhLhdc5Media *media);
BH_LHDC_API uint64_t bh_lhdc5_media_value(BhLhdc5Media *media, int field);
#ifdef __cplusplus
}
#endif
