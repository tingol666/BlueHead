/* BlueHead bounded PCM mixer. Apache-2.0.
 * All allocation happens at creation. The render path never waits for a lock,
 * calls Python, performs I/O, or allocates memory. Stereo float32 throughout.
 */
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdatomic.h>

#define API __declspec(dllexport)
#define INPUTS 8
#define CAPACITY 48064
#define TAPS 64
#define PHASES 1024
typedef struct {
    float *pcm;
    uint32_t read, frames, target, maximum, history;
    uint64_t dropped, rendered;
    int active, buffering;
    float gain, gate, gate_target;
    double phase, drift;
    atomic_uint queued_view, drift_view, gate_view;
    atomic_ullong dropped_view;
} Input;
typedef struct Mixer {
    CRITICAL_SECTION lock;
    Input input[INPUTS];
    atomic_ullong underruns, contentions;
    atomic_uint master_bits;
    int call;
    float sinc[PHASES + 1][TAPS];
} Mixer;

static void publish_input(Input *s) {
    atomic_store_explicit(&s->queued_view, s->frames, memory_order_relaxed);
    atomic_store_explicit(&s->dropped_view, s->dropped, memory_order_relaxed);
    atomic_store_explicit(&s->drift_view, (unsigned)(s->drift * 1000000 + 1000), memory_order_relaxed);
}
API void bh_mix_master(Mixer *m, float gain) {
    if (!m || !isfinite(gain)) return;
    unsigned bits; memcpy(&bits, &gain, sizeof(bits));
    atomic_store_explicit(&m->master_bits, bits, memory_order_relaxed);
}
API float bh_mix_master_value(Mixer *m) {
    unsigned bits = atomic_load_explicit(&m->master_bits, memory_order_relaxed);
    float gain; memcpy(&gain, &bits, sizeof(gain)); return gain;
}

API Mixer *bh_mix_create(void) {
    Mixer *m = calloc(1, sizeof(Mixer));
    if (!m) return NULL;
    InitializeCriticalSection(&m->lock);
    atomic_init(&m->underruns, 0); atomic_init(&m->contentions, 0);
    atomic_init(&m->master_bits, 0x3f800000u);
    m->call = -1;
    /* Band-limited fractional delay, computed once outside the callback.
     * 64-tap Blackman-Harris window, interpolated 1024-phase coefficient table. */
    const double pi = 3.14159265358979323846;
    for (int p = 0; p <= PHASES; p++) {
        double sum = 0;
        for (int t = 0; t < TAPS; t++) {
            double x = t - 31 - (double)p / PHASES;
            double window = fabs(x) >= 32 ? 0 : .35875 + .48829*cos(pi*x/32) + .14128*cos(2*pi*x/32) + .01168*cos(3*pi*x/32);
            double value = fabs(x) < 1e-12 ? 1 : sin(pi*x)/(pi*x);
            m->sinc[p][t] = (float)(value * window); sum += value * window;
        }
        for (int t = 0; t < TAPS; t++) m->sinc[p][t] /= (float)sum;
    }
    for (int i = 0; i < INPUTS; i++) {
        atomic_init(&m->input[i].queued_view, 0); atomic_init(&m->input[i].dropped_view, 0);
        atomic_init(&m->input[i].drift_view, 1000); atomic_init(&m->input[i].gate_view, 0);
        m->input[i].pcm = calloc(CAPACITY * 2, sizeof(float));
        if (!m->input[i].pcm) {
            for (int j = 0; j < i; j++) free(m->input[j].pcm);
            DeleteCriticalSection(&m->lock); free(m); return NULL;
        }
    }
    return m;
}
API void bh_mix_free(Mixer *m) {
    if (!m) return;
    for (int i = 0; i < INPUTS; i++) free(m->input[i].pcm);
    DeleteCriticalSection(&m->lock); free(m);
}
API int bh_mix_add(Mixer *m, uint32_t target, uint32_t maximum) {
    if (!m || maximum > CAPACITY - TAPS || maximum == 0 || target > maximum) return -1;
    EnterCriticalSection(&m->lock);
    int index = -1;
    for (int i = 0; i < INPUTS; i++) if (!m->input[i].active) {
        Input *s = &m->input[i];
        s->active = 1; s->read = s->frames = s->history = 0; s->gain = s->gate = s->gate_target = 1.0f;
        s->target = target; s->maximum = maximum; s->buffering = 1;
        s->dropped = s->rendered = 0; s->phase = s->drift = 0;
        atomic_store(&s->gate_view, 1); publish_input(s);
        index = i; break;
    }
    LeaveCriticalSection(&m->lock); return index;
}
API void bh_mix_remove(Mixer *m, int index) {
    if (!m || index < 0 || index >= INPUTS) return;
    EnterCriticalSection(&m->lock); m->input[index].active = 0;
    if (m->call == index) m->call = -1;
    LeaveCriticalSection(&m->lock);
}
API void bh_mix_clear(Mixer *m, int index) {
    if (!m || index < 0 || index >= INPUTS) return;
    EnterCriticalSection(&m->lock);
    Input *s = &m->input[index];
    s->read = s->frames = s->history = 0; s->buffering = 1; s->phase = s->drift = 0;
    publish_input(s);
    LeaveCriticalSection(&m->lock);
}
API void bh_mix_gain(Mixer *m, int index, float gain) {
    if (!m || index < 0 || index >= INPUTS || !isfinite(gain)) return;
    EnterCriticalSection(&m->lock); m->input[index].gain = gain;
    LeaveCriticalSection(&m->lock);
}
API void bh_mix_gate(Mixer *m, int index, int enabled) {
    if (!m || index < 0 || index >= INPUTS) return;
    enabled = !!enabled;
    if (atomic_load_explicit(&m->input[index].gate_view, memory_order_relaxed) == (unsigned)enabled) return;
    EnterCriticalSection(&m->lock);
    Input *s = &m->input[index];
    s->gate_target = enabled ? 1.0f : 0.0f;
    if (!s->frames || s->buffering) s->gate = s->gate_target;
    atomic_store_explicit(&s->gate_view, (unsigned)enabled, memory_order_relaxed);
    LeaveCriticalSection(&m->lock);
}
API void bh_mix_call(Mixer *m, int index) {
    if (!m) return;
    EnterCriticalSection(&m->lock); m->call = index;
    LeaveCriticalSection(&m->lock);
}
API void bh_mix_write(Mixer *m, int index, const float *pcm, uint32_t frames) {
    if (!m || !pcm || index < 0 || index >= INPUTS) return;
    EnterCriticalSection(&m->lock);
    Input *s = &m->input[index];
    if (!s->active) { LeaveCriticalSection(&m->lock); return; }
    if (frames > s->maximum) { pcm += (frames - s->maximum) * 2; frames = s->maximum; }
    if (s->frames + frames > s->maximum) {
        uint32_t skip = s->frames + frames - s->maximum;
        s->read = (s->read + skip) % CAPACITY; s->frames -= skip;
        s->dropped += skip; s->phase = 0;
    }
    uint32_t write = (s->read + s->frames) % CAPACITY;
    uint32_t first = frames < CAPACITY - write ? frames : CAPACITY - write;
    memcpy(s->pcm + write * 2, pcm, first * 2 * sizeof(float));
    memcpy(s->pcm, pcm + first * 2, (frames - first) * 2 * sizeof(float));
    s->frames += frames;
    publish_input(s);
    LeaveCriticalSection(&m->lock);
}
static float sample(Input *s, int offset, int channel) {
    if (offset < -(int)s->history) offset = -(int)s->history;
    if (offset >= 0 && (uint32_t)offset >= s->frames) offset = (int)s->frames - 1;
    if (offset < 0) return s->pcm[((s->read + CAPACITY + offset) % CAPACITY) * 2 + channel];
    return s->pcm[((s->read + offset) % CAPACITY) * 2 + channel];
}
static float interpolate(Mixer *m, Input *s, int offset, int channel, float fraction) {
    float phase = fraction * PHASES;
    int index = (int)phase;
    float blend = phase - index, value = 0;
    if (index >= PHASES) return sample(s, offset + 1, channel);
    for (int t = 0; t < TAPS; t++) {
        float weight = m->sinc[index][t] + blend * (m->sinc[index+1][t] - m->sinc[index][t]);
        value += sample(s, offset + t - 31, channel) * weight;
    }
    return value;
}
API int bh_mix_render(Mixer *m, float *output, uint32_t count, float master) {
    if (!m || !output || count > CAPACITY || !isfinite(master)) return -1;
    memset(output, 0, count * 2 * sizeof(float));
    if (!TryEnterCriticalSection(&m->lock)) { atomic_fetch_add_explicit(&m->contentions, 1, memory_order_relaxed); return 0; }
    for (int i = 0; i < INPUTS; i++) {
        Input *s = &m->input[i];
        if (!s->active) continue;
        if (s->buffering) {
            if (s->frames < s->target) continue;
            s->buffering = 0;
        }
        float gain = s->gain * ((m->call >= 0 && m->call != i) ? .15f : 1.0f);
        /* Correct only sustained clock drift after two seconds. Band-limited
         * interpolation keeps the tiny +/- 500 ppm correction continuous.
         * Call packets keep their negotiated clock and bounded queue instead. */
        double ratio = 1.0;
        if (s->target && s->rendered > 96000 && m->call != i) {
            double error = ((double)s->frames - s->target) / 48000.0;
            double wanted = error * .02;
            if (wanted > .0005) wanted = .0005;
            if (wanted < -.0005) wanted = -.0005;
            s->drift += .005 * (wanted - s->drift);
            ratio += s->drift;
        }
        uint32_t produced = 0;
        double position = s->phase;
        for (; produced < count && (uint32_t)position < s->frames; produced++, position += ratio) {
            int offset = (int)position;
            float fraction = (float)(position - offset);
            /* Ten-ms envelope, independent of user gain. Gated PCM is still
             * consumed so recovery never replays a stale backlog. */
            if (s->gate < s->gate_target) s->gate = fminf(s->gate_target, s->gate + 1.0f/480);
            else if (s->gate > s->gate_target) s->gate = fmaxf(s->gate_target, s->gate - 1.0f/480);
            for (int ch = 0; ch < 2; ch++) {
                float value = fraction == 0 ? sample(s, offset, ch) : interpolate(m, s, offset, ch, fraction);
                if (!isfinite(value)) value = 0;
                output[produced*2+ch] += value * gain * s->gate;
            }
        }
        uint32_t consumed = (uint32_t)position;
        if (consumed > s->frames) consumed = s->frames;
        s->read = (s->read + consumed) % CAPACITY; s->frames -= consumed;
        s->history = s->history + consumed > 32 ? 32 : s->history + consumed;
        s->phase = s->frames ? position - consumed : 0;
        s->rendered += produced;
        if (produced < count) { s->buffering = 1; m->underruns++; }
        publish_input(s);
    }
    float peak = 0;
    for (uint32_t j = 0; j < count*2; j++) {
        output[j] *= master;
        float magnitude = fabsf(output[j]); if (magnitude > peak) peak = magnitude;
    }
    if (peak > .98f) {
        float scale = .98f / peak;
        for (uint32_t j = 0; j < count*2; j++) output[j] *= scale;
    }
    LeaveCriticalSection(&m->lock); return 1;
}
API uint64_t bh_mix_value(Mixer *m, int index, int field) {
    if (!m) return 0;
    uint64_t value = 0;
    if (index < 0) value = field == 0 ? atomic_load(&m->underruns) : atomic_load(&m->contentions);
    else if (index < INPUTS) {
        Input *s = &m->input[index];
        value = field == 0 ? atomic_load_explicit(&s->queued_view, memory_order_relaxed) :
                field == 1 ? atomic_load_explicit(&s->dropped_view, memory_order_relaxed) :
                atomic_load_explicit(&s->drift_view, memory_order_relaxed);
    }
    return value;
}
