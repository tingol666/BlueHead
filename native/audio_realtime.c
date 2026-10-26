/* BlueHead native PortAudio callbacks and bounded format conversion. Apache-2.0.
 * SpeexDSP is initialized outside callbacks, then keeps its rate/quality fixed.
 * Callbacks allocate nothing and never enter Python. Capture uses a lock-free
 * single-producer/single-consumer ring; its worker owns conversion and routing.
 */
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdatomic.h>
#include "speex_resampler.h"
#include "audio_timeline.h"
#include "audio_float.h"

#define API __declspec(dllexport)
#define BLOCK 480u
#define MAX_IO 48064u
#define CONVERT_BLOCK 1024u
#if ATOMIC_LLONG_LOCK_FREE != 2 || ATOMIC_INT_LOCK_FREE != 2
#error BlueHead realtime audio requires lock-free native atomics
#endif

typedef struct Mixer Mixer;
int bh_mix_render(Mixer *, float *, uint32_t, float);
float bh_mix_master_value(Mixer *);

typedef struct {
  Mixer *mixer;
  SpeexResamplerState *resampler;
  uint32_t rate, offset, remaining;
  float source[BLOCK * 2];
  atomic_int halted;
  int muted;
  atomic_ullong calls, frames, statuses, errors, max_ticks;
  LARGE_INTEGER frequency;
} Render;

static int valid_rate(uint32_t rate) { return rate >= 8000 && rate <= 384000; }
static void maximum(atomic_ullong *value, uint64_t ticks) {
  uint64_t previous = atomic_load_explicit(value, memory_order_relaxed);
  while (ticks > previous &&
         !atomic_compare_exchange_weak_explicit(value, &previous, ticks, memory_order_relaxed,
                                                memory_order_relaxed)) {
  }
}

API Render *bh_render_create(Mixer *mixer, uint32_t rate) {
  if (!mixer || !valid_rate(rate))
    return NULL;
  Render *r = calloc(1, sizeof(Render));
  if (!r)
    return NULL;
  r->mixer = mixer;
  r->rate = rate;
  atomic_init(&r->halted, 0);
  atomic_init(&r->calls, 0);
  atomic_init(&r->frames, 0);
  atomic_init(&r->statuses, 0);
  atomic_init(&r->errors, 0);
  atomic_init(&r->max_ticks, 0);
  QueryPerformanceFrequency(&r->frequency);
  if (rate != 48000) {
    int error = 0;
    r->resampler = speex_resampler_init(2, 48000, rate, 10, &error);
    if (!r->resampler || error) {
      if (r->resampler)
        speex_resampler_destroy(r->resampler);
      free(r);
      return NULL;
    }
    speex_resampler_skip_zeros(r->resampler);
  }
  return r;
}

API void bh_render_halt(Render *r) {
  if (r)
    atomic_store_explicit(&r->halted, 1, memory_order_release);
}
API void bh_render_resume(Render *r) {
  if (r)
    atomic_store_explicit(&r->halted, 0, memory_order_release);
}
API void bh_render_free(Render *r) {
  if (!r)
    return;
  if (r->resampler)
    speex_resampler_destroy(r->resampler);
  free(r);
}
API uint64_t bh_render_value(Render *r, int field) {
  if (!r)
    return 0;
  switch (field) {
  case 0:
    return atomic_load_explicit(&r->calls, memory_order_relaxed);
  case 1:
    return atomic_load_explicit(&r->frames, memory_order_relaxed);
  case 2:
    return atomic_load_explicit(&r->statuses, memory_order_relaxed);
  case 3:
    return atomic_load_explicit(&r->errors, memory_order_relaxed);
  case 4:
    return atomic_load_explicit(&r->max_ticks, memory_order_relaxed) * 1000000 /
           r->frequency.QuadPart;
  case 5:
    return r->resampler ? speex_resampler_get_output_latency(r->resampler) : 0;
  default:
    return 0;
  }
}
static int render_process(Render *r, float *output, uint32_t frames) {
  if (!r || !output || !frames || frames > MAX_IO)
    return -1;
  if (atomic_load_explicit(&r->halted, memory_order_acquire)) {
    memset(output, 0, frames * 2 * sizeof(float));
    return 0;
  }
  float master = bh_mix_master_value(r->mixer);
  if (!r->resampler)
    return bh_mix_render(r->mixer, output, frames, master);
  if (master == 0 && !r->muted) {
    /* Discard already rendered, unmuted conversion input and filter tail.
     * A true mute must also cover the native-format fallback's lookahead. */
    r->offset = r->remaining = 0;
    speex_resampler_reset_mem(r->resampler);
    speex_resampler_skip_zeros(r->resampler);
  }
  r->muted = master == 0;
  uint32_t produced = 0;
  while (produced < frames) {
    if (!r->remaining) {
      bh_mix_render(r->mixer, r->source, BLOCK, master);
      r->offset = 0;
      r->remaining = BLOCK;
    }
    spx_uint32_t input = r->remaining, available = frames - produced;
    int error = speex_resampler_process_interleaved_float(
        r->resampler, r->source + r->offset * 2, &input, output + produced * 2, &available);
    if (error || (!input && !available) || input > r->remaining || available > frames - produced) {
      memset(output + produced * 2, 0, (frames - produced) * 2 * sizeof(float));
      atomic_fetch_add_explicit(&r->errors, 1, memory_order_relaxed);
      return -1;
    }
    r->offset += input;
    r->remaining -= input;
    produced += available;
  }
  if (master == 0)
    memset(output, 0, frames * 2 * sizeof(float));
  return 1;
}

API int bh_render_process(Render *r, float *output, uint32_t frames) {
  unsigned environment = bh_float_enter();
  int result = render_process(r, output, frames);
  bh_float_leave(environment);
  return result;
}
/* PortAudio's ABI: Windows unsigned long is 32-bit, pointers are native-sized. */
API int bh_output_callback(const void *input, void *output, unsigned long frames, const void *times,
                           unsigned long status, void *userdata) {
  (void)input;
  (void)times;
  Render *r = userdata;
  if (!r)
    return 2;
  if (!frames)
    return 0;
  if (!output || frames > MAX_IO)
    return 2;
  LARGE_INTEGER before, after;
  QueryPerformanceCounter(&before);
  if (status)
    atomic_fetch_add_explicit(&r->statuses, 1, memory_order_relaxed);
  if (bh_render_process(r, output, frames) < 0) {
    memset(output, 0, (size_t)frames * 2 * sizeof(float));
  }
  atomic_fetch_add_explicit(&r->calls, 1, memory_order_relaxed);
  atomic_fetch_add_explicit(&r->frames, frames, memory_order_relaxed);
  QueryPerformanceCounter(&after);
  maximum(&r->max_ticks, after.QuadPart - before.QuadPart);
  return 0;
}

typedef struct {
  uint32_t rate, channels, output_rate, capacity;
  float *ring;
  atomic_ullong read, write, dropped, frames, statuses;
  atomic_ullong clock_position, clock_arrival;
  atomic_uint clock_sequence;
  atomic_int halted;
  uint64_t observed_drops;
  SpeexResamplerState *resampler;
  float mono[CONVERT_BLOCK], converted[CONVERT_BLOCK];
  uint32_t offset, remaining;
} Capture;

API Capture *bh_capture_create(uint32_t rate, uint32_t channels, uint32_t output_rate) {
  if (!valid_rate(rate) || channels < 1 || channels > 2 || !valid_rate(output_rate))
    return NULL;
  Capture *c = calloc(1, sizeof(Capture));
  if (!c)
    return NULL;
  c->rate = rate;
  c->channels = channels;
  c->output_rate = output_rate;
  c->capacity = (rate * 60 + 999) / 1000;
  c->ring = calloc((size_t)c->capacity * channels, sizeof(float));
  atomic_init(&c->read, 0);
  atomic_init(&c->write, 0);
  atomic_init(&c->dropped, 0);
  atomic_init(&c->frames, 0);
  atomic_init(&c->statuses, 0);
  atomic_init(&c->halted, 0);
  atomic_init(&c->clock_position, 0);
  atomic_init(&c->clock_arrival, 0);
  atomic_init(&c->clock_sequence, 0);
  if (rate != output_rate) {
    int error = 0;
    c->resampler = speex_resampler_init(1, rate, output_rate, 10, &error);
    if (error) {
      if (c->resampler)
        speex_resampler_destroy(c->resampler);
      free(c->ring);
      free(c);
      return NULL;
    }
  }
  if (!c->ring || (rate != output_rate && !c->resampler)) {
    if (c->resampler)
      speex_resampler_destroy(c->resampler);
    free(c->ring);
    free(c);
    return NULL;
  }
  return c;
}
API void bh_capture_halt(Capture *c, int halted) {
  if (c)
    atomic_store_explicit(&c->halted, !!halted, memory_order_release);
}
/* Reset only while the producer is stopped and the consumer is quiescent. */
API void bh_capture_reset(Capture *c) {
  if (!c)
    return;
  atomic_store(&c->read, atomic_load(&c->write));
  c->remaining = c->offset = 0;
  c->observed_drops = atomic_load(&c->dropped);
  if (c->resampler)
    speex_resampler_reset_mem(c->resampler);
}
API void bh_capture_free(Capture *c) {
  if (!c)
    return;
  if (c->resampler)
    speex_resampler_destroy(c->resampler);
  free(c->ring);
  free(c);
}
API uint64_t bh_capture_value(Capture *c, int field) {
  if (!c)
    return 0;
  switch (field) {
  case 0:
    return atomic_load_explicit(&c->frames, memory_order_relaxed);
  case 1:
    return atomic_load_explicit(&c->statuses, memory_order_relaxed);
  case 2:
    return atomic_load_explicit(&c->dropped, memory_order_relaxed);
  case 3: {
    uint64_t w = atomic_load_explicit(&c->write, memory_order_acquire);
    uint64_t r = atomic_load_explicit(&c->read, memory_order_acquire);
    return w > r ? w - r : 0;
  }
  default:
    return 0;
  }
}
API uint32_t bh_capture_push(Capture *c, const float *input, uint32_t frames) {
  if (!c || frames > MAX_IO || atomic_load_explicit(&c->halted, memory_order_acquire))
    return 0;
  uint64_t w = atomic_load_explicit(&c->write, memory_order_relaxed);
  uint64_t r = atomic_load_explicit(&c->read, memory_order_acquire);
  uint32_t free_frames = c->capacity - (uint32_t)(w - r);
  uint32_t take = frames < free_frames ? frames : free_frames;
  uint32_t at = w % c->capacity;
  uint32_t first = take < c->capacity - at ? take : c->capacity - at;
  if (input) {
    memcpy(c->ring + at * c->channels, input, first * c->channels * sizeof(float));
    memcpy(c->ring, input + first * c->channels, (take - first) * c->channels * sizeof(float));
  } else {
    memset(c->ring + at * c->channels, 0, first * c->channels * sizeof(float));
    memset(c->ring, 0, (take - first) * c->channels * sizeof(float));
  }
  atomic_store_explicit(&c->write, w + take, memory_order_release);
  if (take < frames)
    atomic_fetch_add_explicit(&c->dropped, frames - take, memory_order_release);
  uint64_t position = atomic_fetch_add_explicit(&c->frames, frames, memory_order_relaxed) + frames;
  LARGE_INTEGER now;
  QueryPerformanceCounter(&now);
  /* One producer, bounded snapshot reads. No reader spins on a preempted callback. */
  atomic_fetch_add_explicit(&c->clock_sequence, 1, memory_order_seq_cst);
  atomic_store_explicit(&c->clock_position, bht_scale(position, c->rate, BHT_RATE),
                        memory_order_seq_cst);
  atomic_store_explicit(&c->clock_arrival, now.QuadPart, memory_order_seq_cst);
  atomic_fetch_add_explicit(&c->clock_sequence, 1, memory_order_seq_cst);
  return take;
}
API int bh_capture_clock(Capture *c, uint64_t *position, uint64_t *arrival) {
  if (!c || !position || !arrival)
    return -1;
  for (unsigned attempt = 0; attempt < 3; attempt++) {
    unsigned sequence = atomic_load_explicit(&c->clock_sequence, memory_order_seq_cst);
    if (sequence & 1)
      continue;
    uint64_t p = atomic_load_explicit(&c->clock_position, memory_order_seq_cst);
    uint64_t a = atomic_load_explicit(&c->clock_arrival, memory_order_seq_cst);
    if (sequence == atomic_load_explicit(&c->clock_sequence, memory_order_seq_cst)) {
      *position = p;
      *arrival = a;
      return a ? 1 : 0;
    }
  }
  return 0;
}
API int bh_input_callback(const void *input, void *output, unsigned long frames, const void *times,
                          unsigned long status, void *userdata) {
  (void)output;
  (void)times;
  Capture *c = userdata;
  if (!c)
    return 2;
  if (status)
    atomic_fetch_add_explicit(&c->statuses, 1, memory_order_relaxed);
  bh_capture_push(c, input, frames);
  return 0;
}
static int capture_read_impl(Capture *c, int16_t *integer, float *floating, uint32_t capacity) {
  if (!c || (!integer && !floating) || !capacity || capacity > MAX_IO)
    return -1;
  uint64_t dropped = atomic_load_explicit(&c->dropped, memory_order_acquire);
  if (dropped != c->observed_drops) {
    /* A stalled consumer must discard old speech, then wait for fresh input.
     * The producer never overwrites a slot while this consumer reads it. */
    atomic_store_explicit(&c->read, atomic_load_explicit(&c->write, memory_order_acquire),
                          memory_order_release);
    c->remaining = c->offset = 0;
    c->observed_drops = dropped;
    if (c->resampler)
      speex_resampler_reset_mem(c->resampler);
    return 0;
  }
  uint32_t produced = 0;
  while (produced < capacity) {
    if (!c->remaining) {
      uint64_t r = atomic_load_explicit(&c->read, memory_order_relaxed);
      uint64_t w = atomic_load_explicit(&c->write, memory_order_acquire);
      uint32_t take = (uint32_t)(w - r);
      if (!take)
        break;
      if (take > CONVERT_BLOCK)
        take = CONVERT_BLOCK;
      for (uint32_t i = 0; i < take; i++) {
        uint32_t at = (r + i) % c->capacity;
        float value = c->ring[at * c->channels];
        if (c->channels == 2)
          value = (value + c->ring[at * 2 + 1]) * .5f;
        c->mono[i] = isfinite(value) && fabsf(value) <= 16 ? value : 0;
      }
      atomic_store_explicit(&c->read, r + take, memory_order_release);
      c->remaining = take;
      c->offset = 0;
    }
    spx_uint32_t input = c->remaining;
    spx_uint32_t available = capacity - produced;
    if (available > CONVERT_BLOCK)
      available = CONVERT_BLOCK;
    if (c->resampler) {
      int error = speex_resampler_process_float(c->resampler, 0, c->mono + c->offset, &input,
                                                c->converted, &available);
      if (error || (!input && !available) || input > c->remaining ||
          available > capacity - produced || available > CONVERT_BLOCK)
        return -1;
    } else {
      if (input > available)
        input = available;
      available = input;
      memcpy(c->converted, c->mono + c->offset, available * sizeof(float));
    }
    c->remaining -= input;
    c->offset += input;
    for (uint32_t i = 0; i < available; i++) {
      if (floating) {
        floating[produced + i] = c->converted[i];
        continue;
      }
      float value = c->converted[i] * 32768.0f;
      if (!isfinite(value))
        value = 0;
      if (value > 32767)
        value = 32767;
      if (value < -32768)
        value = -32768;
      integer[produced + i] = (int16_t)lrintf(value);
    }
    produced += available;
  }
  return (int)produced;
}
static int capture_read(Capture *c, int16_t *integer, float *floating, uint32_t capacity) {
  unsigned environment = bh_float_enter();
  int result = capture_read_impl(c, integer, floating, capacity);
  bh_float_leave(environment);
  return result;
}
API int bh_capture_read(Capture *c, int16_t *output, uint32_t capacity) {
  return capture_read(c, output, NULL, capacity);
}
/* Production bridge keeps normalized float PCM until the codec boundary. */
API int bh_capture_read_f32(Capture *c, float *output, uint32_t capacity) {
  return capture_read(c, NULL, output, capacity);
}
