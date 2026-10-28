/* Compressed-SDU duration evidence, independent of transports and render clocks.
 * SPDX-License-Identifier: Apache-2.0
 * No allocation, PCM processing, decoder reset or queue ownership lives here.
 * Unknown SDU durations remain estimates even for a stable sender cadence.
 */
#ifndef BLUEHEAD_AUDIO_GAP_POLICY_H
#define BLUEHEAD_AUDIO_GAP_POLICY_H
#include <stdint.h>
#include <string.h>

#define BHG_HISTORY 32u
typedef enum { BHG_COLD, BHG_VARIABLE, BHG_STABLE } BhGapConfidence;
typedef struct {
  uint32_t frames[BHG_HISTORY], count, write, run_value, run_count;
} BhDurationProfile;
typedef struct {
  uint32_t typical, observed_min, observed_p10, observed_p90, observed_max;
  uint32_t count, support;
  BhGapConfidence confidence;
} BhDurationSummary;
typedef struct {
  uint64_t exact_source_frames, unknown_sdus;
} BhGapEvidence;
/* Stage 1 has no presentation deadline/queue information. Reserved fields
 * cannot influence a decision until a future caller explicitly supplies them. */
typedef struct {
  uint32_t source_rate, exact_budget, estimated_budget, presentation_valid;
  uint64_t presentation_position, presentation_deadline;
} BhGapContext;
typedef struct {
  uint32_t exact_source_frames, estimated_source_frames;
  uint64_t omitted_exact_frames, omitted_unknown_sdus;
  BhDurationSummary duration;
} BhGapPlan;

static inline void bhg_reset(BhDurationProfile *p) { memset(p, 0, sizeof(*p)); }
/* Call once per complete admitted unit, only after it decoded normally. The
 * usual unit is an entire SDU; an independent SBC frame profile supplies orphan
 * fragment estimates. Never mix unit types or train on PLC, incomplete/parser-
 * delayed output, bad or duplicate units. */
static inline void bhg_observe(BhDurationProfile *p, uint32_t frames) {
  if (!frames) return;
  if (p->run_value == frames) {
    if (p->run_count < BHG_HISTORY) ++p->run_count;
  } else { p->run_value = frames; p->run_count = 1; }
  /* A sustained new packetization can replace stale history without changing
   * the decoder, queued PCM or sample clock. An isolated large bundle cannot. */
  if (p->run_count == 8 && p->count >= 8) {
    for (uint32_t i = 0; i < 8; ++i) p->frames[i] = frames;
    p->count = p->write = 8;
    return;
  }
  p->frames[p->write] = frames;
  p->write = (p->write + 1u) % BHG_HISTORY;
  if (p->count < BHG_HISTORY) ++p->count;
}
static inline BhDurationSummary bhg_summary(const BhDurationProfile *p) {
  BhDurationSummary s = {0};
  uint32_t sorted[BHG_HISTORY];
  s.count = p->count;
  if (!s.count) return s;
  memcpy(sorted, p->frames, s.count * sizeof(*sorted));
  for (uint32_t i = 1; i < s.count; ++i) {
    uint32_t value = sorted[i], j = i;
    while (j && sorted[j-1] > value) { sorted[j] = sorted[j-1]; --j; }
    sorted[j] = value;
  }
  s.observed_min = sorted[0]; s.observed_max = sorted[s.count-1];
  s.observed_p10 = sorted[(s.count-1)/10];
  s.observed_p90 = sorted[(s.count-1)*9/10];
  s.typical = sorted[(s.count-1)/2];
  uint32_t best = 0, mode = 0;
  for (uint32_t i = 0; i < s.count;) {
    uint32_t j = i+1;
    while (j < s.count && sorted[j] == sorted[i]) ++j;
    if (j-i > best) { best = j-i; mode = sorted[i]; }
    i = j;
  }
  s.support = best;
  if (s.count < 8) s.confidence = BHG_COLD;
  else if (best * 4u >= s.count * 3u) {
    s.confidence = BHG_STABLE; s.typical = mode;
  } else s.confidence = BHG_VARIABLE;
  return s;
}
static inline BhGapPlan bhg_plan(const BhDurationProfile *profile,
                                 BhGapEvidence evidence, BhGapContext context) {
  BhGapPlan plan = {0};
  plan.duration = bhg_summary(profile);
  if (evidence.exact_source_frames <= context.exact_budget)
    plan.exact_source_frames = (uint32_t)evidence.exact_source_frames;
  else plan.omitted_exact_frames = evidence.exact_source_frames;
  /* A variable profile permits only one unknown SDU per boundary. Even
   * stable history is not proof about missing packets. Cold/variable profiles
   * use <=40ms; stable profiles retain the caller's existing bounded budget.
   * Never manufacture a clipped partial interval and call it exact.
   * Consume omitted evidence once; it must not become future compensation debt.
   */
  uint32_t budget = context.estimated_budget;
  if (plan.omitted_exact_frames) budget = 0;
  /* exact_budget is also the total emission capacity. Exact evidence takes
   * precedence so a mixed plan cannot claim more PCM than its caller can hold. */
  uint32_t remaining = context.exact_budget - plan.exact_source_frames;
  if (budget > remaining) budget = remaining;
  if (budget > context.source_rate / 5u) budget = context.source_rate / 5u;
  if (plan.duration.confidence != BHG_STABLE && budget > context.source_rate / 25u)
    budget = context.source_rate / 25u;
  if (evidence.unknown_sdus && plan.duration.typical &&
      (plan.duration.confidence != BHG_VARIABLE || evidence.unknown_sdus == 1) &&
      evidence.unknown_sdus <= budget / plan.duration.typical) {
    plan.estimated_source_frames = (uint32_t)evidence.unknown_sdus * plan.duration.typical;
  } else plan.omitted_unknown_sdus = evidence.unknown_sdus;
  return plan;
}
#endif
