/* Control-owner format transactions. Apache-2.0.
 * prepare/free may allocate or wait, and must stay outside a PCM bridge.
 * try_commit never allocates, frees, waits, or acquires a blocking lock.
 * Plans retain their native owner, and are single-use after a commit.
 */
#ifndef BLUEHEAD_AUDIO_FORMAT_PLAN_H
#define BLUEHEAD_AUDIO_FORMAT_PLAN_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct BhMixFormatPlan BhMixFormatPlan;
typedef struct BhUpSourcePlan BhUpSourcePlan;
enum {
  BH_FORMAT_COMMITTED = 0,
  BH_FORMAT_STALE = 1,
  BH_FORMAT_BUSY = 2,
  BH_FORMAT_INVALID = -1,
  BH_FORMAT_MEMORY = -2
};
enum { BH_FORMAT_RETIRE_SOURCE = 1u };
/* The first parameter uses the public opaque owner ABI. */
BhMixFormatPlan *bh_mix_format_prepare(void *, int, uint32_t, uint32_t, int *);
int bh_mix_format_try_commit(BhMixFormatPlan *);
void bh_mix_format_plan_free(BhMixFormatPlan *);
BhUpSourcePlan *bh_up_source_prepare(void *, int, uint32_t, uint32_t, int *);
int bh_up_source_try_commit(BhUpSourcePlan *);
void bh_up_source_plan_free(BhUpSourcePlan *);
#ifdef __cplusplus
}
#endif
#endif
