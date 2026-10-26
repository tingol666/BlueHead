/* Offline-only access to the production fractional-delay kernel.
 * Never linked into a product binary; no audio or USB endpoints are opened. */
#include <stdlib.h>
#include <stdatomic.h>
static atomic_llong live_allocations;
static atomic_int allocation_budget = -1;
static void *tracked_calloc(size_t count, size_t size) {
  int budget = atomic_load(&allocation_budget);
  if (budget == 0)
    return NULL;
  if (budget > 0)
    atomic_fetch_sub(&allocation_budget, 1);
  void *pointer = calloc(count, size);
  if (pointer)
    atomic_fetch_add(&live_allocations, 1);
  return pointer;
}
static void tracked_free(void *pointer) {
  if (pointer)
    atomic_fetch_sub(&live_allocations, 1);
  free(pointer);
}
#define calloc tracked_calloc
#define free tracked_free
#include "../native/audio/audio_mixer.c"
#undef calloc
#undef free
API void bh_test_fail_allocation_after(int count) { atomic_store(&allocation_budget, count); }
API long long bh_test_live_allocations(void) { return atomic_load(&live_allocations); }
API int bh_test_fractional_response(Mixer *m, double normalized_frequency, double fraction,
                                    double *real, double *imaginary) {
  if (!m || !real || !imaginary || normalized_frequency < 0 || normalized_frequency >= .5 ||
      fraction < 0 || fraction > 1)
    return -1;
  Input *s = &m->input[0];
  s->cursor = 0;
  s->available = 512;
  for (int i = 0; i < 512; i++) {
    double angle = 6.2831853071795864769 * normalized_frequency * i;
    s->pcm[i * 2] = (float)cos(angle);
    s->pcm[i * 2 + 1] = (float)sin(angle);
  }
  float value[2];
  interpolate_stereo(m->default_route->kernel[0], s, 192, fraction, value);
  *real = value[0];
  *imaginary = value[1];
  return 0;
}
