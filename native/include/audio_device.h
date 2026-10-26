/* BlueHead WASAPI device boundary. Apache-2.0. */
#ifndef BLUEHEAD_AUDIO_DEVICE_H
#define BLUEHEAD_AUDIO_DEVICE_H
#include <stdint.h>
#include <wchar.h>
#ifdef _WIN32
#define BH_DEVICE_API __declspec(dllexport)
#else
#define BH_DEVICE_API
#endif
typedef struct BhDeviceContext BhDeviceContext;
typedef struct BhAudioDevice BhAudioDevice;
enum { BH_DEVICE_ABI = 1, BH_PLAYBACK = 1, BH_CAPTURE = 2, BH_DEVICE_BUFFER_SMALL = -10001 };
/* Stable semantic error classes; native backend codes remain available. */
enum {
  BH_FAILURE_INTERNAL,
  BH_FAILURE_ENDPOINT,
  BH_FAILURE_BUSY,
  BH_FAILURE_PERMISSION,
  BH_FAILURE_FORMAT,
  BH_FAILURE_MEMORY,
  BH_FAILURE_TIMEOUT
};
BH_DEVICE_API int bh_device_error_category(int code);
typedef struct {
  uint32_t size, direction, is_default, reserved;
  wchar_t endpoint_id[64];
  char name[256]; /* UTF-8. */
} BhEndpoint;
typedef struct {
  uint32_t size, abi, direction, sample_rate, channels, output_rate, conversion, active;
  uint64_t callbacks, frames, errors, max_callback_us, notifications, dropped_frames, queued_frames,
      filter_latency_frames;
} BhDeviceStats;
BH_DEVICE_API uint32_t bh_device_abi(void);
/* Does not enumerate/open audio. Loads only an absolute, caller-selected core DLL. */
BH_DEVICE_API BhDeviceContext *bh_device_context_create(const wchar_t *core_path, int *error);
/* Returns busy while a stream still owns the context. Control APIs must be serialized on one
 * thread. */
BH_DEVICE_API int bh_device_context_close(BhDeviceContext *context);
BH_DEVICE_API int bh_device_inventory(BhDeviceContext *context, BhEndpoint *output,
                                      uint32_t capacity, uint32_t *count);
/* Prepared but stopped: opening a candidate never starts a second mixer consumer. */
BH_DEVICE_API BhAudioDevice *bh_device_open_output(BhDeviceContext *context, void *mixer,
                                                   const wchar_t *endpoint_id, int *error);
/* output_rate=0 preserves the actual capture endpoint rate as float PCM. */
BH_DEVICE_API BhAudioDevice *bh_device_open_capture(BhDeviceContext *context,
                                                    const wchar_t *endpoint_id,
                                                    uint32_t output_rate, int *error);
BH_DEVICE_API int bh_device_start(BhAudioDevice *device);
BH_DEVICE_API int bh_device_stop(BhAudioDevice *device);
/* Stop/uninit joins native callbacks before releasing the native render/capture state. */
BH_DEVICE_API int bh_device_close(BhAudioDevice *device);
BH_DEVICE_API int bh_device_active(BhAudioDevice *device);
BH_DEVICE_API int bh_device_stats(BhAudioDevice *device, BhDeviceStats *output);
/* Additional counters preserve the version-1 stats structure ABI. Fields:
 * 0=max callback arrival gap us, 1=max excess over previous block duration us,
 * 2=arrival gaps exceeding previous block duration by more than 2ms.
 * These are observations, not proof that the WASAPI endpoint underflowed. */
BH_DEVICE_API uint64_t bh_device_timing(BhAudioDevice *device, int field);
/* Exactly one consumer; does not call COM or enter the device control thread. */
BH_DEVICE_API int bh_device_read(BhAudioDevice *device, int16_t *output, uint32_t capacity);
/* Normalized mono float PCM; same single-consumer queue as bh_device_read. */
BH_DEVICE_API int bh_device_read_f32(BhAudioDevice *device, float *output, uint32_t capacity);
BH_DEVICE_API int bh_device_clock(BhAudioDevice *device, uint64_t *position, uint64_t *arrival);
/* In-memory callback harness. Never initializes miniaudio or opens audio devices. */
BH_DEVICE_API BhAudioDevice *bh_device_offline(BhDeviceContext *context, uint32_t direction,
                                               void *mixer, uint32_t rate, uint32_t channels,
                                               uint32_t output_rate, int *error);
BH_DEVICE_API int bh_device_offline_process(BhAudioDevice *device, const float *input,
                                            float *output, uint32_t frames);
BH_DEVICE_API void bh_device_ready_event(BhAudioDevice *device, void *event);
#endif
