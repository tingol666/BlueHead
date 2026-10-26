/* Bounded stereo-linked waveform concealment and recovery. Apache-2.0.
 * A short periodic continuation is used only when history is correlated.
 * Uncorrelated/transient material fades quickly. Missing audio is never
 * fabricated indefinitely. Caller owns the state on one serialized worker.
 */
#ifndef BLUEHEAD_AUDIO_CONTINUITY_H
#define BLUEHEAD_AUDIO_CONTINUITY_H
#include <stdint.h>
#include <string.h>
#include <math.h>
#define BHC_HISTORY 2048u
#define BHC_PERIOD 960u
typedef struct {
  float history[BHC_HISTORY * 2], period[BHC_PERIOD * 2];
  uint32_t cursor, count, lag, gap, merge, merge_length;
  float confidence;
  uint64_t concealed, silent, merged;
} BhContinuity;
static void bhc_reset(BhContinuity *c) {
  memset(c, 0, sizeof(*c));
  c->merge_length = 192;
}
static float bhc_at(const BhContinuity *c, uint32_t ago, int channel) {
  return c->history[((c->cursor + BHC_HISTORY - ago) % BHC_HISTORY) * 2 + channel];
}
static uint32_t bhc_period(BhContinuity *c, uint32_t maximum) {
  if (c->count < 192) {
    c->confidence = 0;
    return 0;
  }
  uint32_t limit = c->count - 128;
  if (limit > maximum)
    limit = maximum;
  if (limit > BHC_PERIOD)
    limit = BHC_PERIOD;
  double best = -1;
  uint32_t lag = 0;
  for (uint32_t candidate = 32; candidate <= limit; candidate += 2) {
    double correlation = 1;
    int informative = 0;
    for (int ch = 0; ch < 2; ch++) {
      double xy = 0, xx = 0, yy = 0, sx = 0, sy = 0;
      for (uint32_t j = 1; j <= 128; j++) {
        double a = bhc_at(c, j, ch), b = bhc_at(c, j + candidate, ch);
        xy += a * b;
        xx += a * a;
        yy += b * b;
        sx += a;
        sy += b;
      }
      /* DC/one dominant channel must not turn unpitched stereo detail into
       * a repeated buzz. Score AC correlation independently in both channels,
       * and reject mismatched energy even if normalized correlation is high. */
      double vx = fmax(0, xx - sx * sx / 128), vy = fmax(0, yy - sy * sy / 128);
      if (vx + vy < 1e-12)
        continue;
      informative = 1;
      double score = vx * vy > 1e-20 ? (xy - sx * sy / 128) / sqrt(vx * vy) : -1;
      double error = vx + vy - 2 * (xy - sx * sy / 128);
      if (error > .3 * (vx + vy))
        score = -1;
      correlation = fmin(correlation, score);
    }
    if (!informative)
      correlation = -1;
    /* Prefer the shortest similarly good period; arbitrary long repeats
     * can smear a transient even when a harmonic also correlates. */
    if (correlation > best + .005) {
      best = correlation;
      lag = candidate;
    }
  }
  c->confidence = (float)best;
  if (best < .85)
    return 0;
  for (uint32_t i = 0; i < lag; i++)
    for (int ch = 0; ch < 2; ch++)
      c->period[i * 2 + ch] = bhc_at(c, lag - i, ch);
  return lag;
}
static float bhc_gain(uint32_t elapsed, int periodic) {
  if (!periodic)
    return elapsed < 48 ? 1 - (float)elapsed / 48 : 0;
  if (elapsed <= 240)
    return 1;
  if (elapsed >= 960)
    return 0;
  return .5f + .5f * cosf((float)(elapsed - 240) * (3.14159265358979323846f / 720));
}
static void bhc_prediction(const BhContinuity *c, uint32_t offset, float out[2]) {
  float gain = bhc_gain(offset, c->lag != 0);
  for (int ch = 0; ch < 2; ch++)
    out[ch] = (c->lag     ? c->period[(offset % c->lag) * 2 + ch]
               : c->count ? bhc_at(c, 1, ch)
                          : 0) *
              gain;
}
static void bhc_missing(BhContinuity *c, float out[2]) {
  if (!c->gap)
    c->lag = bhc_period(c, BHC_PERIOD);
  bhc_prediction(c, c->gap, out);
  c->gap++;
  c->concealed++;
  if (fabsf(out[0]) + fabsf(out[1]) < 1e-10f)
    c->silent++;
}
static void bhc_real(BhContinuity *c, const float in[2], float out[2]) {
  if (c->gap && !c->merge)
    c->merge = c->merge_length = 192;
  if (c->merge) {
    float prediction[2];
    bhc_prediction(c, c->gap, prediction);
    float alpha = 1 - (float)(c->merge - 1) / c->merge_length;
    for (int ch = 0; ch < 2; ch++)
      out[ch] = prediction[ch] * (1 - alpha) + in[ch] * alpha;
    c->merge--;
    c->gap++;
    c->merged++;
    if (!c->merge)
      c->gap = 0;
  } else {
    out[0] = in[0];
    out[1] = in[1];
  }
  for (int ch = 0; ch < 2; ch++)
    c->history[c->cursor * 2 + ch] = isfinite(in[ch]) ? in[ch] : 0;
  c->cursor = (c->cursor + 1) % BHC_HISTORY;
  if (c->count < BHC_HISTORY)
    c->count++;
}
#endif
