/* Sample-continuous gain and stereo-linked lookahead limiting. Apache-2.0.
 * State belongs to the single render consumer. No allocation, locks or OS calls.
 * Ordinary unity-level PCM is unchanged apart from a fixed 72-frame delay.
 * Per-source delay ownership prevents cleared/removed audio leaking into a new
 * stream and avoids flushing an unrelated source during a device transition.
 */
#ifndef BLUEHEAD_AUDIO_QUALITY_H
#define BLUEHEAD_AUDIO_QUALITY_H
#include <math.h>
#include <stdint.h>
#include <string.h>
#define BHQ_LOOKAHEAD 72u      /* Public diagnostics use the canonical 48k clock. */
#define BHQ_MAX_LOOKAHEAD 576u /* 1.5ms at the maximum 384k PCM rate. */
#define BHQ_WINDOW (BHQ_MAX_LOOKAHEAD + 1u)
#define BHQ_GAIN_RAMP 240u /* 5ms; true zero/mute remains immediate. */

typedef struct {
  double value, target, step;
  uint32_t remaining;
  int initialized;
} BhGainRamp;
typedef struct {
  BhGainRamp gain;
  double delay[BHQ_MAX_LOOKAHEAD * 2];
  uint32_t at, rate, lookahead, ramp;
} BhSourceQuality;
typedef struct {
  BhGainRamp master;
  double master_delay[BHQ_MAX_LOOKAHEAD], peak[BHQ_WINDOW];
  /* A float envelope stalls ~1e-4 below unity during its slow release.
   * Keep control precision separate from float PCM so release is transparent. */
  double gain;
  uint64_t sequence[BHQ_WINDOW], frame, limited;
  uint32_t at, head, size, rate, lookahead, ramp;
  double attack, release;
} BhMixQuality;

static void bhq_target(BhGainRamp *r, float target, uint32_t ramp) {
  if (!r->initialized || target == 0) {
    r->value = r->target = target;
    r->remaining = 0;
    r->initialized = 1;
  } else if (target != r->target) {
    r->target = target;
    r->step = (target - r->value) / ramp;
    r->remaining = ramp;
  }
}
static double bhq_gain(BhGainRamp *r) {
  if (r->remaining) {
    r->value += r->step;
    if (!--r->remaining)
      r->value = r->target;
  }
  return r->value;
}
static void bhq_source_clear(BhSourceQuality *s) {
  memset(s->delay, 0, sizeof(s->delay));
  s->at = 0;
}
static void bhq_source(BhSourceQuality *s, const float *input, double *raw, double *delayed,
                       uint32_t count, float target) {
  if (target == 0 && s->gain.value != 0)
    bhq_source_clear(s);
  bhq_target(&s->gain, target, s->ramp ? s->ramp : BHQ_GAIN_RAMP);
  for (uint32_t j = 0; j < count; j++) {
    double gain = bhq_gain(&s->gain);
    for (int ch = 0; ch < 2; ch++) {
      double value = input[j * 2 + ch] * gain;
      /* Guard malformed PCM without poisoning the persistent peak envelope.
       * This bound is far above valid decoded audio or supported 200% gain. */
      if (!isfinite(value))
        value = 0;
      value = fmin(64, fmax(-64, value));
      raw[j * 2 + ch] += value;
      delayed[j * 2 + ch] += s->delay[s->at * 2 + ch];
      s->delay[s->at * 2 + ch] = value;
    }
    if (++s->at == (s->lookahead ? s->lookahead : BHQ_LOOKAHEAD))
      s->at = 0;
  }
}
static void bhq_reset_envelope(BhMixQuality *q) {
  q->head = q->size = q->at = 0;
  q->frame = 0;
  q->gain = 1;
  memset(q->master_delay, 0, sizeof(q->master_delay));
}
static void bhq_process(BhMixQuality *q, const double *delayed, const double *raw, float *output,
                        uint32_t count, float master) {
  uint32_t lookahead = q->lookahead ? q->lookahead : BHQ_LOOKAHEAD;
  bhq_target(&q->master, master, q->ramp ? q->ramp : BHQ_GAIN_RAMP);
  for (uint32_t j = 0; j < count; j++, q->frame++) {
    double gain = bhq_gain(&q->master), peak = 0;
    double old_master = q->master_delay[q->at];
    q->master_delay[q->at] = gain;
    if (++q->at == lookahead)
      q->at = 0;
    for (int ch = 0; ch < 2; ch++)
      peak = fmax(peak, fabs(raw[j * 2 + ch] * gain));
    /* Monotonic deque: amortized O(1), inclusive [n-72,n] lookahead for
     * output sample n-72, with the same envelope across callback boundaries. */
    while (q->size && q->frame - q->sequence[q->head] > lookahead) {
      q->head = (q->head + 1) % BHQ_WINDOW;
      q->size--;
    }
    while (q->size && q->peak[(q->head + q->size - 1) % BHQ_WINDOW] <= peak)
      q->size--;
    uint32_t at = (q->head + q->size) % BHQ_WINDOW;
    q->peak[at] = peak;
    q->sequence[at] = q->frame;
    q->size++;
    double future_peak = q->peak[q->head];
    double wanted = future_peak > 1 ? 1 / future_peak : 1;
    /* 0.15ms attack, 70ms release. Lookahead provides ten attack time
     * constants before the peak reaches output. No normal-level compression. */
    double coefficient =
        wanted < q->gain ? (q->rate ? q->attack : .870324726) : (q->rate ? q->release : .999702425);
    q->gain = wanted + coefficient * (q->gain - wanted);
    double out[2] = {delayed[j * 2] * old_master, delayed[j * 2 + 1] * old_master};
    double present_peak = fmax(fabs(out[0]), fabs(out[1]));
    double limit = present_peak > 1 ? 1 / present_peak : 1;
    double scale = fmin(q->gain, limit);
    if (scale < .999999f)
      q->limited++;
    for (int ch = 0; ch < 2; ch++)
      output[j * 2 + ch] = master == 0 ? 0 : (float)(out[ch] * scale);
  }
}
#endif
