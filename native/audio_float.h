/* Bounded DSP floating-point environment. Apache-2.0.
 * Denormals (<1.18e-38, about -758 dBFS) must not turn near-silence into a
 * callback CPU spike. Save/restore the caller's complete environment; do not
 * enable fast-math, change rounding or flush audible low-level float PCM. */
#ifndef BLUEHEAD_AUDIO_FLOAT_H
#define BLUEHEAD_AUDIO_FLOAT_H
#if defined(__SSE2__) || defined(_M_X64)
#include <xmmintrin.h>
static inline unsigned bh_float_enter(void) {
  unsigned previous = _mm_getcsr();
  _mm_setcsr(previous | 0x8040u); /* FTZ and DAZ only. */
  return previous;
}
static inline void bh_float_leave(unsigned previous) { _mm_setcsr(previous); }
#else
static inline unsigned bh_float_enter(void) { return 0; }
static inline void bh_float_leave(unsigned previous) { (void)previous; }
#endif
#endif
