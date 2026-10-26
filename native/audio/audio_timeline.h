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
typedef struct {
  uint32_t previous, rate;
  uint64_t extended;
  int valid;
} BhPacketClock;
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
/* Observe original producer timing, independently of when a worker drains it.
 * Timestamp gaps carry their source duration, so loss is not counted again as
 * arrival jitter. Equal positions (RTP fragments) do not create new clocks. */
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
/* Unsigned RTP wrap is extended once; reordered old timestamps are ignored.
 * A caller resets this state at a stream/SSRC/format boundary. */
static inline uint64_t bht_packet(BhPacketClock *c, uint32_t timestamp, uint32_t rate) {
  if (!c->valid || c->rate != rate) {
    c->previous = timestamp;
    c->extended = 0;
    c->rate = rate;
    c->valid = 1;
  } else {
    int32_t delta = (int32_t)(timestamp - c->previous);
    if (delta > 0) {
      c->extended += (uint32_t)delta;
      c->previous = timestamp;
    }
  }
  return bht_scale(c->extended, rate, BHT_RATE);
}
#endif
