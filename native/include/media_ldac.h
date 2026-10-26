/* BlueHead experimental LDAC media worker ABI. Apache-2.0. */
#ifndef BLUEHEAD_MEDIA_LDAC_H
#define BLUEHEAD_MEDIA_LDAC_H
#include <stdint.h>
#ifdef _WIN32
#define BH_LDAC_API __declspec(dllexport)
#else
#define BH_LDAC_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
typedef struct BhLdacMedia BhLdacMedia;
BH_LDAC_API BhLdacMedia *bh_ldac_media_create(int rate, int channels, int output_rate);
BH_LDAC_API void bh_ldac_media_destroy(BhLdacMedia *);
BH_LDAC_API int bh_ldac_media_restart(BhLdacMedia *);
BH_LDAC_API int bh_ldac_media_feed(BhLdacMedia *, const uint8_t *, uint32_t);
BH_LDAC_API uint32_t bh_ldac_media_read(BhLdacMedia *, float *, uint32_t);
BH_LDAC_API void bh_ldac_media_discard(BhLdacMedia *);
/* Same fields as media_engine.h; no decoder state crosses process boundaries. */
BH_LDAC_API uint64_t bh_ldac_media_value(BhLdacMedia *, int);
#ifdef __cplusplus
}
#endif
#endif
