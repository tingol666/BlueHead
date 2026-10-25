/* BlueHead native A2DP decoder ABI. Apache-2.0. One worker owns each handle. */
#ifndef BLUEHEAD_MEDIA_ENGINE_H
#define BLUEHEAD_MEDIA_ENGINE_H
#include <stdint.h>
#ifdef _WIN32
#define BH_MEDIA_API __declspec(dllexport)
#else
#define BH_MEDIA_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
typedef struct BhMedia BhMedia;
/* codec: 1 SBC, 2 AAC-LATM, 3 aptX, 4 aptX HD. LDAC remains isolated. */
BH_MEDIA_API BhMedia *bh_media_create(int codec, int rate, int channels, int output_rate);
BH_MEDIA_API void bh_media_destroy(BhMedia *);
/* Keep AAC StreamMuxConfig on resume; discard queued PCM and packet fragments. */
BH_MEDIA_API int bh_media_restart(BhMedia *);
BH_MEDIA_API int bh_media_feed(BhMedia *, const uint8_t *, uint32_t size);
BH_MEDIA_API uint32_t bh_media_read(BhMedia *, float *stereo, uint32_t frames);
BH_MEDIA_API uint32_t bh_media_drain_mixer(BhMedia *, void *mixer, int index);
BH_MEDIA_API void bh_media_discard(BhMedia *);
/* 0 frames,1 wire bytes,2 coded bytes,3 codec errors,4 recoveries,5 RTP lost,
 * 6 synchronizing,7 queued frames,8 queue drops,9 malformed packets. */
BH_MEDIA_API uint64_t bh_media_value(BhMedia *, int field);
#ifdef __cplusplus
}
#endif
#endif
