/* BlueHead native WASAPI device I/O. Apache-2.0.
 * Only the control owner calls miniaudio lifecycle APIs. Native callbacks never
 * allocate, block, call Python or invoke device control. Core DLL references
 * outlive every callback. Capture conversion is drained by one non-RT consumer.
 */
#include <windows.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#define MA_ENABLE_ONLY_SPECIFIC_BACKENDS
#define MA_ENABLE_WASAPI
#define MA_NO_NULL
#define MA_NO_DECODING
#define MA_NO_ENCODING
#define MA_NO_ENGINE
#define MA_NO_RESOURCE_MANAGER
#define MA_NO_NODE_GRAPH
#define MA_NO_GENERATION
#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio.h"
#include "audio_device.h"

#define MAX_FRAMES 48064u
#define CHUNK 4096u
#if ATOMIC_LLONG_LOCK_FREE != 2 || ATOMIC_INT_LOCK_FREE != 2
#error BlueHead device callbacks require lock-free atomics
#endif
typedef void *(*RenderCreate)(void *, uint32_t);
typedef int (*ProcessRender)(void *, float *, uint32_t);
typedef void (*FreeState)(void *);
typedef uint64_t (*StateValue)(void *, int);
typedef void *(*CaptureCreate)(uint32_t, uint32_t, uint32_t);
typedef uint32_t (*CapturePush)(void *, const float *, uint32_t);
typedef int (*CaptureRead)(void *, int16_t *, uint32_t);
struct BhDeviceContext {
    HMODULE core;
    ma_context context;
    int initialized;
    uint32_t streams;
    RenderCreate render_create;
    ProcessRender render_process;
    FreeState render_free, capture_free, capture_reset;
    StateValue render_value, capture_value;
    CaptureCreate capture_create;
    CapturePush capture_push;
    CaptureRead capture_read;
};
struct BhAudioDevice {
    BhDeviceContext *owner;
    ma_device device;
    void *state;
    uint32_t direction, rate, channels, output_rate, conversion;
    int initialized, offline, offline_active;
    atomic_ullong callbacks, frames, errors, max_ticks, notifications;
    LARGE_INTEGER frequency;
};
static void set_error(int *error, int value) { if (error) *error = value; }
static int ensure_context(BhDeviceContext *context) {
    if (context->initialized) return MA_SUCCESS;
    ma_backend backend = ma_backend_wasapi;
    ma_context_config config = ma_context_config_init();
    ma_result result = ma_context_init(&backend, 1, &config, &context->context);
    if (result == MA_SUCCESS) context->initialized = 1;
    return result;
}
BH_DEVICE_API uint32_t bh_device_abi(void) { return BH_DEVICE_ABI; }
BH_DEVICE_API BhDeviceContext *bh_device_context_create(const wchar_t *path, int *error) {
    set_error(error, MA_INVALID_ARGS);
    /* A relative DLL path must never search the current working directory. */
    if (!path || !((path[0] && path[1] == L':' && (path[2] == L'\\' || path[2] == L'/')) || (path[0] == L'\\' && path[1] == L'\\'))) return NULL;
    BhDeviceContext *context = calloc(1, sizeof(*context));
    if (!context) { set_error(error, MA_OUT_OF_MEMORY); return NULL; }
    context->core = LoadLibraryExW(path, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!context->core) { free(context); set_error(error, MA_FAILED_TO_OPEN_BACKEND_DEVICE); return NULL; }
#define LOAD(member, name) do { FARPROC p = GetProcAddress(context->core, name); memcpy(&context->member, &p, sizeof(p)); if (!p) goto missing; } while (0)
    LOAD(render_create, "bh_render_create"); LOAD(render_process, "bh_render_process"); LOAD(render_free, "bh_render_free"); LOAD(render_value, "bh_render_value");
    LOAD(capture_create, "bh_capture_create"); LOAD(capture_push, "bh_capture_push"); LOAD(capture_read, "bh_capture_read"); LOAD(capture_free, "bh_capture_free");
    LOAD(capture_reset, "bh_capture_reset"); LOAD(capture_value, "bh_capture_value");
#undef LOAD
    set_error(error, MA_SUCCESS); return context;
missing:
    FreeLibrary(context->core); free(context); set_error(error, MA_INVALID_DATA); return NULL;
}
BH_DEVICE_API int bh_device_context_close(BhDeviceContext *context) {
    if (!context) return MA_SUCCESS;
    if (context->streams) return MA_BUSY;
    if (context->initialized) ma_context_uninit(&context->context);
    FreeLibrary(context->core); free(context); return MA_SUCCESS;
}
BH_DEVICE_API int bh_device_inventory(BhDeviceContext *context, BhEndpoint *output, uint32_t capacity, uint32_t *count) {
    if (!context || !count || (capacity && !output)) return MA_INVALID_ARGS;
    int result = ensure_context(context); if (result != MA_SUCCESS) return result;
    ma_device_info *playback = NULL, *capture = NULL; ma_uint32 pcount = 0, ccount = 0;
    result = ma_context_get_devices(&context->context, &playback, &pcount, &capture, &ccount);
    if (result != MA_SUCCESS) return result;
    *count = pcount + ccount;
    if (capacity < *count) return BH_DEVICE_BUFFER_SMALL;
    for (uint32_t i = 0; i < *count; i++) {
        ma_device_info *info = i < pcount ? &playback[i] : &capture[i-pcount];
        BhEndpoint *item = &output[i]; memset(item, 0, sizeof(*item));
        item->size = sizeof(*item); item->direction = i < pcount ? BH_PLAYBACK : BH_CAPTURE; item->is_default = info->isDefault;
        memcpy(item->endpoint_id, info->id.wasapi, sizeof(item->endpoint_id)); item->endpoint_id[63] = 0;
        memcpy(item->name, info->name, sizeof(item->name)); item->name[255] = 0;
    }
    return MA_SUCCESS;
}
static void record_max(atomic_ullong *destination, uint64_t value) {
    uint64_t previous = atomic_load_explicit(destination, memory_order_relaxed);
    while (value > previous && !atomic_compare_exchange_weak_explicit(destination, &previous, value, memory_order_relaxed, memory_order_relaxed)) {}
}
static void process_audio(BhAudioDevice *stream, const float *input, float *output, uint32_t count) {
    if (!stream || !stream->state || !count) return;
    LARGE_INTEGER before, after; QueryPerformanceCounter(&before);
    uint32_t at = 0;
    while (at < count) {
        uint32_t take = count-at < CHUNK ? count-at : CHUNK;
        if (stream->direction == BH_PLAYBACK) {
            if (stream->owner->render_process(stream->state, output + (size_t)at*2, take) < 0) {
                memset(output + (size_t)at*2, 0, (size_t)take*2*sizeof(float)); atomic_fetch_add_explicit(&stream->errors, 1, memory_order_relaxed);
            }
        } else stream->owner->capture_push(stream->state, input ? input + (size_t)at*stream->channels : NULL, take);
        at += take;
    }
    atomic_fetch_add_explicit(&stream->callbacks, 1, memory_order_relaxed); atomic_fetch_add_explicit(&stream->frames, count, memory_order_relaxed);
    QueryPerformanceCounter(&after); record_max(&stream->max_ticks, after.QuadPart-before.QuadPart);
}
static void on_data(ma_device *device, void *output, const void *input, ma_uint32 count) {
    BhAudioDevice *stream = device->pUserData;
    if (stream->direction == BH_PLAYBACK && !output) return;
    process_audio(stream, input, output, count);
}
static void on_notification(const ma_device_notification *notification) {
    BhAudioDevice *stream = notification->pDevice->pUserData;
    atomic_fetch_add_explicit(&stream->notifications, 1, memory_order_relaxed);
}
static BhAudioDevice *allocate_stream(BhDeviceContext *context, uint32_t direction) {
    BhAudioDevice *stream = calloc(1, sizeof(*stream)); if (!stream) return NULL;
    stream->owner = context; stream->direction = direction; QueryPerformanceFrequency(&stream->frequency);
    atomic_init(&stream->callbacks, 0); atomic_init(&stream->frames, 0); atomic_init(&stream->errors, 0);
    atomic_init(&stream->max_ticks, 0); atomic_init(&stream->notifications, 0); return stream;
}
static int create_state(BhAudioDevice *stream, void *mixer) {
    BhDeviceContext *context = stream->owner;
    stream->state = stream->direction == BH_PLAYBACK ? context->render_create(mixer, stream->rate) : context->capture_create(stream->rate, stream->channels, stream->output_rate);
    return stream->state ? MA_SUCCESS : MA_OUT_OF_MEMORY;
}
static BhAudioDevice *open_device(BhDeviceContext *context, uint32_t direction, void *mixer, const wchar_t *endpoint, uint32_t output_rate, int *error) {
    set_error(error, MA_INVALID_ARGS);
    if (!context || (direction == BH_PLAYBACK && !mixer) || output_rate < 8000 || output_rate > 384000) return NULL;
    ma_device_id id; memset(&id, 0, sizeof(id));
    if (endpoint && *endpoint) {
        size_t length = wcslen(endpoint); if (length >= 64) return NULL;
        memcpy(id.wasapi, endpoint, (length+1)*sizeof(wchar_t));
    }
    int result = ensure_context(context); if (result != MA_SUCCESS) { set_error(error, result); return NULL; }
    BhAudioDevice *stream = allocate_stream(context, direction);
    if (!stream) { set_error(error, MA_OUT_OF_MEMORY); return NULL; }
    stream->channels = direction == BH_PLAYBACK ? 2 : 1; stream->output_rate = output_rate;
    for (uint32_t mode = 0; mode < 2; mode++) {
        ma_device_config config = ma_device_config_init(direction == BH_PLAYBACK ? ma_device_type_playback : ma_device_type_capture);
        config.sampleRate = mode == 0 ? 48000 : 0;
        config.periodSizeInMilliseconds = 10; config.noFixedSizedCallback = MA_TRUE;
        config.dataCallback = on_data; config.notificationCallback = on_notification; config.pUserData = stream;
        config.wasapi.usage = ma_wasapi_usage_pro_audio; config.wasapi.noHardwareOffloading = MA_TRUE;
        config.wasapi.noAutoConvertSRC = mode != 0;
        /* The control owner follows defaults transactionally. Hidden reroutes
         * cannot change the rate underneath our already initialized Speex state. */
        config.wasapi.noAutoStreamRouting = MA_TRUE;
        if (direction == BH_PLAYBACK) {
            config.playback.pDeviceID = endpoint && *endpoint ? &id : NULL;
            config.playback.format = ma_format_f32; config.playback.channels = 2;
        } else {
            config.capture.pDeviceID = endpoint && *endpoint ? &id : NULL;
            config.capture.format = ma_format_f32; config.capture.channels = 1;
        }
        result = ma_device_init(&context->context, &config, &stream->device);
        if (result != MA_SUCCESS) continue;
        /* Some endpoints reject Windows automatic SRC and miniaudio can then
         * negotiate a different internal rate. Do not silently activate its
         * generic converter: reopen at the native clock and use our stateful
         * SpeexDSP path, preserving the 1.2.4 discontinuity fix. */
        uint32_t internal_rate = direction == BH_PLAYBACK ? stream->device.playback.internalSampleRate : stream->device.capture.internalSampleRate;
        if (internal_rate != stream->device.sampleRate) {
            ma_device_uninit(&stream->device); result = MA_FORMAT_NOT_SUPPORTED; continue;
        }
        stream->initialized = 1; stream->rate = stream->device.sampleRate; stream->conversion = mode;
        result = create_state(stream, mixer);
        if (result == MA_SUCCESS) { context->streams++; set_error(error, MA_SUCCESS); return stream; }
        ma_device_uninit(&stream->device); stream->initialized = 0; break;
    }
    free(stream); set_error(error, result); return NULL;
}
BH_DEVICE_API BhAudioDevice *bh_device_open_output(BhDeviceContext *context, void *mixer, const wchar_t *endpoint, int *error) { return open_device(context, BH_PLAYBACK, mixer, endpoint, 48000, error); }
BH_DEVICE_API BhAudioDevice *bh_device_open_capture(BhDeviceContext *context, const wchar_t *endpoint, uint32_t output_rate, int *error) { return open_device(context, BH_CAPTURE, NULL, endpoint, output_rate, error); }
BH_DEVICE_API int bh_device_start(BhAudioDevice *stream) {
    if (!stream) return MA_INVALID_ARGS;
    if (stream->offline) { stream->offline_active = 1; return MA_SUCCESS; }
    return ma_device_start(&stream->device);
}
BH_DEVICE_API int bh_device_stop(BhAudioDevice *stream) {
    if (!stream) return MA_INVALID_ARGS;
    int result;
    if (stream->offline) { stream->offline_active = 0; result = MA_SUCCESS; }
    else result = ma_device_stop(&stream->device);
    /* The caller serializes this with the capture consumer. Never replay speech
     * buffered before a stop/restart or a failed route transaction. */
    if (result == MA_SUCCESS && stream->direction == BH_CAPTURE) stream->owner->capture_reset(stream->state);
    return result;
}
BH_DEVICE_API int bh_device_close(BhAudioDevice *stream) {
    if (!stream) return MA_SUCCESS;
    if (stream->initialized) ma_device_uninit(&stream->device);
    if (stream->state) {
        if (stream->direction == BH_PLAYBACK) stream->owner->render_free(stream->state);
        else stream->owner->capture_free(stream->state);
    }
    stream->owner->streams--; free(stream); return MA_SUCCESS;
}
BH_DEVICE_API int bh_device_active(BhAudioDevice *stream) { return stream ? (stream->offline ? stream->offline_active : (int)ma_device_is_started(&stream->device)) : 0; }
BH_DEVICE_API int bh_device_stats(BhAudioDevice *stream, BhDeviceStats *out) {
    if (!stream || !out || out->size != sizeof(*out)) return MA_INVALID_ARGS;
    memset(out, 0, sizeof(*out)); out->size = sizeof(*out); out->abi = BH_DEVICE_ABI; out->direction = stream->direction;
    out->sample_rate = stream->rate; out->channels = stream->channels; out->output_rate = stream->output_rate;
    out->conversion = stream->conversion; out->active = bh_device_active(stream);
    out->callbacks = atomic_load_explicit(&stream->callbacks, memory_order_relaxed); out->frames = atomic_load_explicit(&stream->frames, memory_order_relaxed);
    out->errors = atomic_load_explicit(&stream->errors, memory_order_relaxed);
    out->max_callback_us = atomic_load_explicit(&stream->max_ticks, memory_order_relaxed)*1000000 / stream->frequency.QuadPart;
    out->notifications = atomic_load_explicit(&stream->notifications, memory_order_relaxed);
    if (stream->direction == BH_CAPTURE) {
        out->dropped_frames = stream->owner->capture_value(stream->state, 2); out->queued_frames = stream->owner->capture_value(stream->state, 3);
    } else out->filter_latency_frames = stream->owner->render_value(stream->state, 5);
    return MA_SUCCESS;
}
BH_DEVICE_API int bh_device_read(BhAudioDevice *stream, int16_t *output, uint32_t capacity) {
    if (!stream || stream->direction != BH_CAPTURE || !output || !capacity || capacity > MAX_FRAMES) return MA_INVALID_ARGS;
    return stream->owner->capture_read(stream->state, output, capacity);
}
BH_DEVICE_API BhAudioDevice *bh_device_offline(BhDeviceContext *context, uint32_t direction, void *mixer, uint32_t rate, uint32_t channels, uint32_t output_rate, int *error) {
    set_error(error, MA_INVALID_ARGS);
    if (!context || (direction != BH_PLAYBACK && direction != BH_CAPTURE) || (direction == BH_PLAYBACK && (!mixer || channels != 2)) ||
        channels < 1 || channels > 2 || rate < 8000 || rate > 384000 || output_rate < 8000 || output_rate > 384000) return NULL;
    BhAudioDevice *stream = allocate_stream(context, direction); if (!stream) { set_error(error, MA_OUT_OF_MEMORY); return NULL; }
    stream->offline = 1; stream->rate = rate; stream->channels = channels; stream->output_rate = output_rate; stream->conversion = rate == 48000 ? 0 : 1;
    int result = create_state(stream, mixer); if (result != MA_SUCCESS) { free(stream); set_error(error, result); return NULL; }
    context->streams++; set_error(error, MA_SUCCESS); return stream;
}
BH_DEVICE_API int bh_device_offline_process(BhAudioDevice *stream, const float *input, float *output, uint32_t frames) {
    if (!stream || !stream->offline || frames > MAX_FRAMES || (stream->direction == BH_PLAYBACK && !output)) return MA_INVALID_ARGS;
    process_audio(stream, input, output, frames); return MA_SUCCESS;
}
