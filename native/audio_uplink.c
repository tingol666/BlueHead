/* BlueHead native microphone/computer-audio gain and mixing. Apache-2.0.
 * Called by capture/SCO workers, not the endpoint callback. Bounded, no allocation
 * after creation. The two capture rings never read media render/loopback PCM.
 */
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdatomic.h>
#define API __declspec(dllexport)
#define LIMIT 34560u
typedef struct {
  uint8_t data[LIMIT];
  uint32_t read, size;
} Ring;
typedef struct Mixer Mixer;
Mixer *bh_mix_create(void);
void bh_mix_free(Mixer *);
int bh_mix_add(Mixer *, uint32_t, uint32_t);
void bh_mix_remove(Mixer *, int);
void bh_mix_clear(Mixer *, int);
void bh_mix_gain(Mixer *, int, float);
void bh_mix_write_s16_mono(Mixer *, int, const int16_t *, uint32_t);
int bh_mix_render(Mixer *, float *, uint32_t, float);
uint64_t bh_mix_value(Mixer *, int, int);
int bh_mix_clock_config(Mixer *, float, float);
int bh_mix_continuity(Mixer *, int, int, uint32_t, uint32_t);
static unsigned bits(float f) {
  unsigned u;
  memcpy(&u, &f, 4);
  return u;
}
static float number(unsigned u) {
  float f;
  memcpy(&f, &u, 4);
  return f;
}
typedef struct {
  CRITICAL_SECTION lock;
  Ring mic, system;
  uint32_t maximum, trim, rate;
  atomic_uint mic_gain, system_gain, device_gain;
  atomic_int muted, adaptive;
  atomic_ullong excess[2];
  Mixer *clock;
  int input[2];
  float stereo[LIMIT];
  int16_t a[LIMIT / 2], b[LIMIT / 2];
} Uplink;
API Uplink *bh_up_create(void) {
  Uplink *u = calloc(1, sizeof(*u));
  if (!u)
    return NULL;
  InitializeCriticalSection(&u->lock);
  u->maximum = 16000;
  u->trim = 8000;
  atomic_init(&u->mic_gain, bits(1));
  atomic_init(&u->system_gain, bits(1));
  atomic_init(&u->device_gain, bits(1));
  atomic_init(&u->muted, 0);
  atomic_init(&u->adaptive, 0);
  atomic_init(&u->excess[0], 0);
  atomic_init(&u->excess[1], 0);
  u->rate = 16000;
  u->clock = bh_mix_create();
  if (!u->clock) {
    DeleteCriticalSection(&u->lock);
    free(u);
    return NULL;
  }
  bh_mix_clock_config(u->clock, 3, 1000);
  u->input[0] = bh_mix_add(u->clock, 1440, 4320);
  u->input[1] = bh_mix_add(u->clock, 1440, 4320);
  if (u->input[0] < 0 || u->input[1] < 0) {
    bh_mix_free(u->clock);
    DeleteCriticalSection(&u->lock);
    free(u);
    return NULL;
  }
  return u;
}
API void bh_up_free(Uplink *u) {
  if (u) {
    bh_mix_free(u->clock);
    DeleteCriticalSection(&u->lock);
    free(u);
  }
}
static void reset_bus(Uplink *u, int bus) {
  bh_mix_clear(u->clock, u->input[bus]);
  atomic_store(&u->excess[bus], 0);
}
API void bh_up_clear(Uplink *u, int bus) {
  if (!u)
    return;
  EnterCriticalSection(&u->lock);
  if (bus != 1)
    u->mic.read = u->mic.size = 0;
  if (bus != 0)
    u->system.read = u->system.size = 0;
  if (bus != 1)
    reset_bus(u, 0);
  if (bus != 0)
    reset_bus(u, 1);
  LeaveCriticalSection(&u->lock);
}
API int bh_up_rate(Uplink *u, uint32_t rate) {
  if (!u || rate < 8000 || rate > 192000)
    return -1;
  EnterCriticalSection(&u->lock);
  atomic_store(&u->adaptive, 0);
  u->rate = rate;
  u->maximum = rate * 18 / 100;
  u->trim = rate * 12 / 100;
  u->mic.read = u->mic.size = u->system.read = u->system.size = 0;
  reset_bus(u, 0);
  reset_bus(u, 1);
  LeaveCriticalSection(&u->lock);
  return 0;
}
/* Explicit production-only 48k capture mode. Legacy byte FIFO remains available
 * for low-rate protocol oracles and callers not requesting clock matching. */
API int bh_up_adaptive(Uplink *u, int enabled) {
  if (!u || (enabled && u->rate != 48000))
    return -1;
  EnterCriticalSection(&u->lock);
  reset_bus(u, 0);
  reset_bus(u, 1);
  u->mic.read = u->mic.size = u->system.read = u->system.size = 0;
  atomic_store_explicit(&u->adaptive, !!enabled, memory_order_release);
  LeaveCriticalSection(&u->lock);
  return 0;
}
API void bh_up_gain(Uplink *u, float mic, float system, float device, int muted) {
  if (!u || !isfinite(mic) || !isfinite(system) || !isfinite(device))
    return;
  atomic_store(&u->mic_gain, bits(mic));
  atomic_store(&u->system_gain, bits(system));
  atomic_store(&u->device_gain, bits(device));
  atomic_store(&u->muted, !!muted);
}
API uint32_t bh_up_size(Uplink *u, int bus) {
  if (!u)
    return 0;
  if (atomic_load_explicit(&u->adaptive, memory_order_acquire))
    return (uint32_t)bh_mix_value(u->clock, u->input[!!bus], 0) * 2;
  EnterCriticalSection(&u->lock);
  uint32_t size = bus ? u->system.size : u->mic.size;
  LeaveCriticalSection(&u->lock);
  return size;
}
API void bh_up_write(Uplink *u, int bus, const uint8_t *data, uint32_t size) {
  if (!u || !data || !size)
    return;
  if (atomic_load_explicit(&u->adaptive, memory_order_acquire)) {
    if (size & 1)
      return;
    if (size > 8640) {
      atomic_fetch_add(&u->excess[!!bus], (size - 8640) / 2);
      data += size - 8640;
      size = 8640;
    }
    bh_mix_write_s16_mono(u->clock, u->input[!!bus], (const int16_t *)data, size / 2);
    return;
  }
  EnterCriticalSection(&u->lock);
  Ring *r = bus ? &u->system : &u->mic;
  if (size > u->maximum) {
    data += size - u->trim;
    size = u->trim;
    r->read = r->size = 0;
  } else if (r->size + size > u->maximum) {
    uint32_t keep = u->trim > size ? u->trim - size : 0;
    if (keep > r->size)
      keep = r->size;
    r->read = (r->read + r->size - keep) % LIMIT;
    r->size = keep;
    if (size > u->trim) {
      data += size - u->trim;
      size = u->trim;
    }
  }
  uint32_t at = (r->read + r->size) % LIMIT, first = size < LIMIT - at ? size : LIMIT - at;
  memcpy(r->data + at, data, first);
  memcpy(r->data, data + first, size - first);
  r->size += size;
  LeaveCriticalSection(&u->lock);
}
static uint32_t take(Ring *r, uint8_t *out, uint32_t size) {
  uint32_t count = size < r->size ? size : r->size,
           first = count < LIMIT - r->read ? count : LIMIT - r->read;
  memcpy(out, r->data + r->read, first);
  memcpy(out + first, r->data, count - first);
  memset(out + count, 0, size - count);
  r->read = (r->read + count) % LIMIT;
  r->size -= count;
  return count;
}
API int bh_up_read(Uplink *u, uint8_t *output, uint32_t size) {
  if (!u || !output || size > LIMIT)
    return -1;
  float mg = number(atomic_load(&u->mic_gain)), sg = number(atomic_load(&u->system_gain)),
        dg = number(atomic_load(&u->device_gain));
  int muted = atomic_load(&u->muted);
  if (atomic_load_explicit(&u->adaptive, memory_order_acquire)) {
    if (size & 1)
      return -1;
    bh_mix_gain(u->clock, u->input[0], mg);
    bh_mix_gain(u->clock, u->input[1], sg);
    if (bh_mix_render(u->clock, u->stereo, size / 2, muted ? 0 : dg) < 0)
      return -1;
    for (uint32_t i = 0; i < size / 2; i++) {
      float value = u->stereo[i * 2] * 32768;
      if (!isfinite(value))
        value = 0;
      if (value > 32767)
        value = 32767;
      if (value < -32768)
        value = -32768;
      int16_t sample = (int16_t)lrintf(value);
      memcpy(output + i * 2, &sample, 2);
    }
    return (int)size;
  }
  EnterCriticalSection(&u->lock);
  if (muted) {
    u->mic.read = u->mic.size = u->system.read = u->system.size = 0;
    memset(output, 0, size);
    LeaveCriticalSection(&u->lock);
    return 0;
  }
  take(&u->mic, (uint8_t *)u->a, size);
  uint32_t system = take(&u->system, (uint8_t *)u->b, size);
  if (mg == 1 && dg == 1 && !system)
    memcpy(output, u->a, size);
  else {
    float peak = 0;
    for (uint32_t i = 0; i < size / 2; i++) {
      float value = (u->a[i] * mg + u->b[i] * sg) * dg;
      if (fabsf(value) > peak)
        peak = fabsf(value);
    }
    float limit = peak > 32767 ? 32767 / peak : 1;
    for (uint32_t i = 0; i < size / 2; i++) {
      float value = (u->a[i] * mg + u->b[i] * sg) * dg * limit;
      long rounded = lrintf(value);
      if (rounded > 32767)
        rounded = 32767;
      if (rounded < -32768)
        rounded = -32768;
      int16_t sample = (int16_t)rounded;
      memcpy(output + i * 2, &sample, 2);
    }
    if (size & 1)
      output[size - 1] = 0;
  }
  LeaveCriticalSection(&u->lock);
  return (int)size;
}
/* fields: queued frames, dropped frames, drift+1000 ppm, underrun events,
 * initial/recovery prefill frames. Only explicit starvation enters rebuffer. */
API uint64_t bh_up_value(Uplink *u, int bus, int field) {
  if (!u || bus < -1 || bus > 1 || field < 0 || field > 4)
    return 0;
  if (bus == -1)
    return bh_mix_value(u->clock, -1, field);
  if (!atomic_load_explicit(&u->adaptive, memory_order_acquire))
    return field == 0 ? bh_up_size(u, bus) / 2 : field == 2 ? 1000 : 0;
  uint64_t value = bh_mix_value(u->clock, u->input[bus], field);
  return field == 1 ? value + atomic_load(&u->excess[bus]) : value;
}

API int bh_up_continuity(Uplink *u, int enabled) {
  if (!u)
    return -1;
  for (int bus = 0; bus < 2; bus++)
    if (bh_mix_continuity(u->clock, u->input[bus], enabled, 720, 2880) < 0)
      return -1;
  return 0;
}
