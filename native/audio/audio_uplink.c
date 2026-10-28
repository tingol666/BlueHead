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
#include "audio_timeline.h"
#include "audio_format_plan.h"
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
void bh_mix_write_f32_mono(Mixer *, int, const float *, uint32_t);
int bh_mix_render(Mixer *, float *, uint32_t, float);
uint64_t bh_mix_value(Mixer *, int, int);
int bh_mix_clock_config(Mixer *, float, float);
int bh_mix_continuity(Mixer *, int, int, uint32_t, uint32_t);
int bh_mix_budget(Mixer *, int, uint32_t);
int bh_mix_timeline(Mixer *, int, int);
void bh_mix_observe(Mixer *, int, uint64_t, uint64_t);
int bh_mix_format(Mixer *, int, uint32_t);
typedef struct MixRoute MixRoute;
MixRoute *bh_mix_route_create(Mixer *, uint32_t);
void bh_mix_route_free(MixRoute *);
int bh_mix_render_route(MixRoute *, float *, uint32_t, float);
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
  MixRoute *route;
  atomic_uint source_rate[2];
  atomic_ullong source_revision[2];
  atomic_uint references, closing;
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
  atomic_init(&u->source_rate[0], 48000);
  atomic_init(&u->source_rate[1], 48000);
  atomic_init(&u->source_revision[0], 1);
  atomic_init(&u->source_revision[1], 1);
  atomic_init(&u->references, 1);
  atomic_init(&u->closing, 0);
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
  u->route = bh_mix_route_create(u->clock, 48000);
  if (!u->route) {
    bh_mix_free(u->clock);
    DeleteCriticalSection(&u->lock);
    free(u);
    return NULL;
  }
  return u;
}
static void release_uplink(Uplink *u) {
  if (atomic_fetch_sub_explicit(&u->references,1,memory_order_acq_rel)==1) {
    bh_mix_route_free(u->route);
    bh_mix_free(u->clock);
    DeleteCriticalSection(&u->lock);
    free(u);
  }
}
API void bh_up_free(Uplink *u) {
  if (u && !atomic_exchange(&u->closing,1)) release_uplink(u);
}
/* Control-side format contracts. Output changes require the voice consumer
 * stopped; a source change uses the mixer's per-source reader lease. */
API int bh_up_output_rate(Uplink *u, uint32_t rate) {
  if (!u || rate < 8000 || rate > 384000)
    return -1;
  MixRoute *prepared = bh_mix_route_create(u->clock, rate);
  if (!prepared)
    return -1;
  bh_mix_route_free(u->route);
  u->route = prepared;
  return 0;
}
struct BhUpSourcePlan {
  Uplink *owner;
  BhMixFormatPlan *format;
  int bus, committed;
  uint32_t rate, old_rate, flags;
  uint64_t revision;
};
API void bh_up_source_plan_free(BhUpSourcePlan *p) {
  if(!p) return;
  Uplink *u=p->owner;
  bh_mix_format_plan_free(p->format);
  free(p);
  release_uplink(u);
}
API BhUpSourcePlan *bh_up_source_prepare(void *opaque,int bus,uint32_t rate,
                                        uint32_t flags,int *error) {
  if(error)*error=BH_FORMAT_INVALID;
  Uplink *u=opaque;
  if(!u || bus<0 || bus>1 || rate<8000 || rate>384000 ||
     (flags&~BH_FORMAT_RETIRE_SOURCE)) return NULL;
  BhUpSourcePlan *p=calloc(1,sizeof(*p));
  if(!p) {if(error)*error=BH_FORMAT_MEMORY;return NULL;}
  atomic_fetch_add(&u->references,1);
  p->owner=u;p->bus=bus;p->rate=rate;p->flags=flags;
  EnterCriticalSection(&u->lock);
  int valid=!atomic_load(&u->closing);
  p->revision=atomic_load(&u->source_revision[bus]);
  p->old_rate=atomic_load(&u->source_rate[bus]);
  int index=u->input[bus];
  LeaveCriticalSection(&u->lock);
  /* In particular, do not hold u->lock while the mixer allocates/prepares:
   * legacy TX readers and unrelated capture producers share that owner. */
  if(valid) p->format=bh_mix_format_prepare(u->clock,index,rate,flags,error);
  if(!p->format) {
    if(!valid && error)*error=BH_FORMAT_STALE;
    bh_up_source_plan_free(p);return NULL;
  }
  return p;
}
API int bh_up_source_try_commit(BhUpSourcePlan *p) {
  if(!p || p->committed) return BH_FORMAT_INVALID;
  Uplink *u=p->owner;
  if(!TryEnterCriticalSection(&u->lock)) return BH_FORMAT_BUSY;
  int result=BH_FORMAT_STALE;
  if(atomic_load(&u->closing) || p->revision!=atomic_load(&u->source_revision[p->bus]) ||
     p->old_rate!=atomic_load(&u->source_rate[p->bus])) goto done;
  result=bh_mix_format_try_commit(p->format);
  if(result==BH_FORMAT_COMMITTED) {
    if(p->rate!=p->old_rate || p->flags) {
      atomic_store(&u->source_rate[p->bus],p->rate);
      atomic_store(&u->excess[p->bus],0);
      Ring *r=p->bus?&u->system:&u->mic;
      r->read=r->size=0;
      atomic_fetch_add(&u->source_revision[p->bus],1);
    }
    p->committed=1;
  }
done:
  LeaveCriticalSection(&u->lock);return result;
}
API int bh_up_source_rate(Uplink *u,int bus,uint32_t rate) {
  if(!u) return -1;
  atomic_fetch_add(&u->references,1);
  int output=-1;
  for(unsigned attempt=0;attempt<8;++attempt) {
    int error;
    BhUpSourcePlan *p=bh_up_source_prepare(u,bus,rate,0,&error);
    if(!p) break;
    int result;
    while((result=bh_up_source_try_commit(p))==BH_FORMAT_BUSY) Sleep(0);
    bh_up_source_plan_free(p);
    if(result==BH_FORMAT_COMMITTED) {output=0;break;}
    if(result!=BH_FORMAT_STALE || atomic_load(&u->closing)) break;
  }
  release_uplink(u);
  return output;
}
static void reset_bus(Uplink *u, int bus) {
  bh_mix_clear(u->clock, u->input[bus]);
  atomic_store(&u->excess[bus], 0);
  atomic_fetch_add(&u->source_revision[bus],1);
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
    uint32_t maximum = atomic_load(&u->source_rate[!!bus]) * 9 / 100 * 2;
    if (size > maximum) {
      atomic_fetch_add(&u->excess[!!bus], (size - maximum) / 2);
      data += size - maximum;
      size = maximum;
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
API int bh_up_write_f32(Uplink *u, int bus, const float *data, uint32_t frames) {
  if (!u || !data || bus < 0 || bus > 1 ||
      !atomic_load_explicit(&u->adaptive, memory_order_acquire))
    return -1;
  uint32_t maximum = atomic_load(&u->source_rate[bus]) * 9 / 100;
  if (frames > maximum) {
    atomic_fetch_add(&u->excess[bus], frames - maximum);
    data += frames - maximum;
    frames = maximum;
  }
  bh_mix_write_f32_mono(u->clock, u->input[bus], data, frames);
  return (int)frames;
}
static int render_adaptive(Uplink *u, uint32_t frames) {
  float mg = number(atomic_load(&u->mic_gain)), sg = number(atomic_load(&u->system_gain)),
        dg = number(atomic_load(&u->device_gain));
  bh_mix_gain(u->clock, u->input[0], mg);
  bh_mix_gain(u->clock, u->input[1], sg);
  return bh_mix_render_route(u->route, u->stereo, frames, atomic_load(&u->muted) ? 0 : dg);
}
API int bh_up_read_f32(Uplink *u, float *output, uint32_t frames) {
  if (!u || !output || frames > LIMIT / 2 ||
      !atomic_load_explicit(&u->adaptive, memory_order_acquire))
    return -1;
  if (render_adaptive(u, frames) < 0)
    return -1;
  for (uint32_t i = 0; i < frames; i++)
    output[i] = u->stereo[i * 2];
  return (int)frames;
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
    if (render_adaptive(u, size / 2) < 0)
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
  if (!u || bus < -1 || bus > 1 || field < 0 || field > 20)
    return 0;
  if (bus == -1)
    return bh_mix_value(u->clock, -1, field);
  if (!atomic_load_explicit(&u->adaptive, memory_order_acquire))
    return field == 0 ? bh_up_size(u, bus) / 2 : field == 2 ? 1000 : 0;
  uint64_t value = bh_mix_value(u->clock, u->input[bus], field);
  return field == 1 ? value + bht_scale(atomic_load(&u->excess[bus]),
                                        atomic_load(&u->source_rate[bus]), BHT_RATE)
                    : value;
}

API int bh_up_continuity(Uplink *u, int enabled) {
  if (!u)
    return -1;
  for (int bus = 0; bus < 2; bus++) {
    if (bh_mix_continuity(u->clock, u->input[bus], enabled, 720, 2880) < 0)
      return -1;
    bh_mix_timeline(u->clock, u->input[bus], enabled ? BHT_CAPTURE : BHT_GENERIC);
  }
  return 0;
}
API int bh_up_budget(Uplink *u, uint32_t frames, int fixed) {
  if (!u || frames > 3840) return -1;
  for (int bus = 0; bus < 2; bus++) {
    if (bh_mix_budget(u->clock, u->input[bus], frames) < 0 ||
        bh_mix_continuity(u->clock, u->input[bus], 1, fixed ? frames : 720, fixed ? frames : 2880) < 0)
      return -1;
  }
  return 0;
}
API void bh_up_observe(Uplink *u, int bus, uint64_t position, uint64_t arrival) {
  if (u && bus >= 0 && bus < 2)
    bh_mix_observe(u->clock, u->input[bus], position, arrival);
}
