/* Common software audio clock and presentation contract. Apache-2.0.
 * Rate controller adapted from PipeWire rate-control.h, MIT, Copyright (c)
 * 2022 Pauli Virtanen. Pinned source/license: vendor/pipewire-rate-control.
 * Positions are 64-bit frames on the canonical 48k timebase. PCM rates are
 * independent; rate-control queue/duration/period must share source-frame units.
 */
#ifndef BLUEHEAD_AUDIO_TIMELINE_H
#define BLUEHEAD_AUDIO_TIMELINE_H
#include <stdint.h>
#include <math.h>
#define BHT_RATE 48000u
enum { BHT_GENERIC = 0, BHT_MEDIA = 1, BHT_VOICE = 2, BHT_CAPTURE = 3 };
typedef struct {
  double average, ratio;
} BhAudioClock;
typedef struct {
  uint64_t position, arrival;
  double peak;
  int valid;
} BhAudioArrival;
static inline uint64_t bht_scale(uint64_t frames, uint32_t from, uint32_t to) {
  return frames / from * to + frames % from * to / from;
}
static inline void bht_clock_reset(BhAudioClock *c, double target) {
  c->average = target;
  c->ratio = 1;
}
static inline double bht_clock_update(BhAudioClock *c, double available, double duration,
                                      double target, double period, double limit) {
  double beta = fmin(.5, duration / period);
  double old = c->average;
  double average = beta * available + (1 - beta) * old;
  c->ratio += (average - old) / (3 * period) + beta * (old - target) / (27 * period);
  c->ratio = fmin(1 + limit, fmax(1 - limit, c->ratio));
  c->average = average;
  return c->ratio;
}
/* Observe original producer timing independently of worker drain scheduling.
 * Only known PCM intervals can measure jitter. Estimated/omitted intervals
 * reanchor through bht_arrival_confident; they are not an exact source clock. */
static inline double bht_arrival(BhAudioArrival *c, uint64_t position, uint64_t arrival,
                                 uint64_t frequency) {
  if (!arrival || !frequency)
    return c->peak;
  if (c->valid && position > c->position && arrival >= c->arrival) {
    double seconds = (double)(arrival - c->arrival) / frequency;
    double late = fmax(0, seconds * BHT_RATE - (double)(position - c->position));
    c->peak = fmax(fmin(late, 4800), c->peak * exp(-seconds / 5));
  } else if (c->valid && position == c->position) {
    return c->peak;
  } else {
    c->peak = 0;
  }
  c->position = position;
  c->arrival = arrival;
  c->valid = 1;
  return c->peak;
}
static inline double bht_arrival_confident(BhAudioArrival *c, uint64_t position,
                                          uint64_t arrival, uint64_t frequency,
                                          uint32_t uncertain) {
  if (!uncertain) return bht_arrival(c, position, arrival, frequency);
  if (!arrival || !frequency || (c->valid && arrival < c->arrival)) return c->peak;
  if (c->valid) c->peak *= exp(-(double)(arrival-c->arrival)/frequency/5);
  c->position = position; c->arrival = arrival; c->valid = 1;
  return c->peak;
}
#endif
