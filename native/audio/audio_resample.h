/* One-pass rate conversion and clock matching. Apache-2.0.
 * Immutable kernels are prepared by the control owner. Hermite interpolation
 * of double-precision coefficients avoids a large phase bank and preserves
 * low-level PCM. No allocation, locks or coefficient design on the callback.
 */
#ifndef BLUEHEAD_AUDIO_RESAMPLE_H
#define BLUEHEAD_AUDIO_RESAMPLE_H
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#define BHR_PHASES 256u
#define BHR_BASE_TAPS 128u
#define BHR_MAX_TAPS 6144u /* 384k -> 8k, bounded public PCM domain. */
#define BHR_HISTORY (BHR_MAX_TAPS / 2u)
typedef struct BhRateKernel {
  uint32_t input_rate, output_rate, taps, centre, references;
  double ratio;
  double *coefficient; /* [phase][tap][value, derivative per phase step] */
} BhRateKernel;
static void bhr_free(BhRateKernel *k) {
  if (k) {
    free(k->coefficient);
    free(k);
  }
}
static BhRateKernel *bhr_create(uint32_t input_rate, uint32_t output_rate) {
  if (input_rate < 8000 || input_rate > 384000 || output_rate < 8000 || output_rate > 384000)
    return NULL;
  BhRateKernel *k = calloc(1, sizeof(*k));
  if (!k)
    return NULL;
  k->input_rate = input_rate;
  k->output_rate = output_rate;
  k->ratio = (double)input_rate / output_rate;
  k->taps = ((uint32_t)ceil(BHR_BASE_TAPS * fmax(1, k->ratio)) + 1u) & ~1u;
  k->centre = k->taps / 2u - 1u;
  if (k->taps > BHR_MAX_TAPS) {
    bhr_free(k);
    return NULL;
  }
  k->coefficient = calloc((size_t)(BHR_PHASES + 1u) * k->taps * 2u, sizeof(double));
  if (!k->coefficient) {
    bhr_free(k);
    return NULL;
  }
  const double pi = 3.14159265358979323846;
  /* Downsampling needs an explicit anti-alias filter even at integer phase.
   * 92% of output Nyquist leaves a transition band without attenuating the
   * 20kHz band at 48k. Equal/up rates keep the full source passband. */
  double cutoff = k->ratio > 1 ? .92 / k->ratio : 1;
  double radius = k->taps / 2.;
  for (uint32_t p = 0; p <= BHR_PHASES; p++) {
    double sum = 0, slope = 0;
    double *row = k->coefficient + (size_t)p * k->taps * 2;
    for (uint32_t t = 0; t < k->taps; t++) {
      double x = (double)t - k->centre - (double)p / BHR_PHASES;
      double value = 0, derivative = 0;
      if (fabs(x) < radius) {
        double a = pi * x / radius;
        double window = .35875 + .48829 * cos(a) + .14128 * cos(2 * a) + .01168 * cos(3 * a);
        double dw = -pi / radius * (.48829 * sin(a) + .28256 * sin(2 * a) + .03504 * sin(3 * a));
        double sinc = fabs(x) < 1e-9 ? cutoff : sin(pi * cutoff * x) / (pi * x);
        double ds =
            fabs(x) < 1e-9
                ? 0
                : (pi * cutoff * x * cos(pi * cutoff * x) - sin(pi * cutoff * x)) / (pi * x * x);
        value = sinc * window;
        derivative = -(ds * window + sinc * dw);
      }
      row[t * 2] = value;
      row[t * 2 + 1] = derivative;
      sum += value;
      slope += derivative;
    }
    for (uint32_t t = 0; t < k->taps; t++) {
      double value = row[t * 2], derivative = row[t * 2 + 1];
      row[t * 2] = value / sum;
      row[t * 2 + 1] = (derivative * sum - value * slope) / (sum * sum * BHR_PHASES);
    }
  }
  return k;
}
#endif
