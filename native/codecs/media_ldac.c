/* Bounded RTP -> LDAC -> continuous SpeexDSP -> stereo float32 pipeline.
 * The untrusted experimental decoder must only be loaded in the child process.
 * Existing pinned libldacdec supplies decoding; pinned SpeexDSP supplies SRC.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "media_ldac.h"
#include <speex_resampler.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define QUEUE 48064u
#define MAX_PACKET 65535u
#define MAX_OUTPUT 32768u
typedef void *(*DecoderCreate)(void);
typedef void (*DecoderFree)(void *);
typedef int (*DecoderDecode)(void *, const uint8_t *, int, float *, int);
struct BhLdacMedia {
  HMODULE library;
  DecoderCreate create;
  DecoderFree destroy;
  DecoderDecode decode;
  void *decoder;
  SpeexResamplerState *resampler;
  int rate, output_rate, have_sequence, recovering;
  uint16_t sequence;
  uint32_t read, queued;
  uint64_t frames, bytes, coded, errors, recoveries, lost, dropped, malformed;
  float queue[QUEUE * 2], packet[MAX_OUTPUT * 2], decoded[512];
};

static int load_decoder(BhLdacMedia *m) {
  HMODULE own = NULL;
  wchar_t path[32768];
  if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                              GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                          (LPCWSTR)(uintptr_t)&bh_ldac_media_create, &own))
    return -1;
  DWORD size = GetModuleFileNameW(own, path, 32768);
  if (!size || size >= 32768)
    return -1;
  wchar_t *slash = wcsrchr(path, L'\\');
  if (!slash || (size_t)(slash - path) + 19 >= 32768)
    return -1;
  wcscpy(slash + 1, L"bluehead_ldac.dll");
  m->library =
      LoadLibraryExW(path, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
  if (!m->library)
    return -1;
  FARPROC create = GetProcAddress(m->library, "bh_ldac_create");
  FARPROC destroy = GetProcAddress(m->library, "bh_ldac_free");
  FARPROC decode = GetProcAddress(m->library, "bh_ldac_decode");
  if (!create || !destroy || !decode)
    return -1;
  _Static_assert(sizeof(create) == sizeof(m->create), "Windows function pointer size");
  memcpy(&m->create, &create, sizeof(create));
  memcpy(&m->destroy, &destroy, sizeof(destroy));
  memcpy(&m->decode, &decode, sizeof(decode));
  return 0;
}

static int reset_decoder(BhLdacMedia *m) {
  if (m->decoder)
    m->destroy(m->decoder);
  m->decoder = m->create();
  if (!m->decoder)
    return -1;
  if (m->rate != m->output_rate) {
    /* Pinned SpeexDSP reset_mem does not clear channel-strided history for
     * this interleaved stereo layout. A fresh state avoids old right-channel
     * samples after a pause/error, without restarting the isolated process. */
    if (m->resampler)
      speex_resampler_destroy(m->resampler);
    int error = 0;
    m->resampler = speex_resampler_init(2, m->rate, m->output_rate, 10, &error);
    if (error || !m->resampler || speex_resampler_skip_zeros(m->resampler))
      return -1;
  }
  return 0;
}

BH_LDAC_API void bh_ldac_media_destroy(BhLdacMedia *m) {
  if (!m)
    return;
  if (m->decoder && m->destroy)
    m->destroy(m->decoder);
  if (m->resampler)
    speex_resampler_destroy(m->resampler);
  if (m->library)
    FreeLibrary(m->library);
  free(m);
}

BH_LDAC_API BhLdacMedia *bh_ldac_media_create(int rate, int channels, int output_rate) {
  if ((rate != 44100 && rate != 48000 && rate != 88200 && rate != 96000) || channels != 2 ||
      output_rate < 8000 || output_rate > 192000)
    return NULL;
  BhLdacMedia *m = calloc(1, sizeof(*m));
  if (!m)
    return NULL;
  m->rate = rate;
  m->output_rate = output_rate;
  if (load_decoder(m) < 0) {
    bh_ldac_media_destroy(m);
    return NULL;
  }
  if (reset_decoder(m) < 0) {
    bh_ldac_media_destroy(m);
    return NULL;
  }
  return m;
}

BH_LDAC_API void bh_ldac_media_discard(BhLdacMedia *m) {
  if (m)
    m->read = m->queued = 0;
}

BH_LDAC_API int bh_ldac_media_restart(BhLdacMedia *m) {
  if (!m)
    return -1;
  m->read = m->queued = m->have_sequence = m->recovering = 0;
  return reset_decoder(m);
}

static int invalid(BhLdacMedia *m) {
  m->malformed++;
  return -1;
}

static void enqueue(BhLdacMedia *m, uint32_t frames) {
  const float *data = m->packet;
  m->frames += frames;
  if (frames > QUEUE) {
    data += (frames - QUEUE) * 2;
    m->dropped += frames - QUEUE;
    frames = QUEUE;
  }
  if (m->queued + frames > QUEUE) {
    uint32_t skip = m->queued + frames - QUEUE;
    m->read = (m->read + skip) % QUEUE;
    m->queued -= skip;
    m->dropped += skip;
  }
  uint32_t at = (m->read + m->queued) % QUEUE;
  uint32_t first = frames < QUEUE - at ? frames : QUEUE - at;
  memcpy(m->queue + at * 2, data, first * 2 * sizeof(float));
  memcpy(m->queue, data + first * 2, (frames - first) * 2 * sizeof(float));
  m->queued += frames;
}

BH_LDAC_API int bh_ldac_media_feed(BhLdacMedia *m, const uint8_t *data, uint32_t size) {
  static const int rates[] = {44100, 48000, 88200, 96000};
  if (!m || !data)
    return -1;
  m->bytes += size;
  if (size < 12 || size > MAX_PACKET || data[0] >> 6 != 2)
    return invalid(m);
  uint32_t offset = 12 + 4 * (data[0] & 15), end = size;
  if (offset > size)
    return invalid(m);
  if (data[0] & 0x10) {
    if (offset + 4 > size)
      return invalid(m);
    offset += 4 + 4 * (((uint32_t)data[offset + 2] << 8) | data[offset + 3]);
  }
  if (offset >= end)
    return invalid(m);
  if (data[0] & 0x20) {
    uint32_t padding = data[size - 1];
    if (!padding || padding > size - offset)
      return invalid(m);
    end -= padding;
  }
  if (offset >= end)
    return invalid(m);
  uint16_t sequence = (uint16_t)((data[2] << 8) | data[3]);
  if (m->have_sequence) {
    uint16_t distance = (uint16_t)(sequence - m->sequence);
    if (!distance || distance > 0x8000)
      return 0;
    if (distance > 1)
      m->lost += distance - 1;
  }
  m->sequence = sequence;
  m->have_sequence = 1;
  data += offset;
  size = end - offset;
  if (!size || data[0] & 0xE0 || !(data[0] & 31))
    return invalid(m);
  uint32_t count = data[0] & 31, positions[31], lengths[31], at = 1;
  /* Validate the entire packet before emitting any samples. */
  for (uint32_t i = 0; i < count; i++) {
    if (size - at < 3 || data[at] != 0xAA)
      return invalid(m);
    uint32_t header = ((uint32_t)data[at] << 16) | ((uint32_t)data[at + 1] << 8) | data[at + 2];
    uint32_t rate_id = (header >> 13) & 7, channel_id = (header >> 11) & 3;
    uint32_t length = ((header >> 2) & 511) + 4;
    if (rate_id >= 4 || channel_id != 2 || rates[rate_id] != m->rate || length > size - at)
      return invalid(m);
    positions[i] = at;
    lengths[i] = length;
    at += length;
  }
  if (at != size)
    return invalid(m);
  m->coded += size - 1;
  uint32_t produced = 0;
  for (uint32_t i = 0; i < count; i++) {
    int frames = m->decode(m->decoder, data + positions[i], (int)lengths[i], m->decoded, 512);
    if (frames != (m->rate >= 88200 ? 256 : 128))
      goto damaged;
    for (int sample = 0; sample < frames * 2; sample++)
      if (!isfinite(m->decoded[sample]))
        goto damaged;
    if (m->resampler) {
      spx_uint32_t input = (spx_uint32_t)frames, output = MAX_OUTPUT - produced;
      if (speex_resampler_process_interleaved_float(m->resampler, m->decoded, &input,
                                                    m->packet + produced * 2, &output) ||
          input != (spx_uint32_t)frames)
        goto damaged;
      produced += output;
    } else {
      if ((uint32_t)frames > MAX_OUTPUT - produced)
        goto damaged;
      memcpy(m->packet + produced * 2, m->decoded, (size_t)frames * 2 * sizeof(float));
      produced += (uint32_t)frames;
    }
  }
  for (uint32_t i = 0; i < produced * 2; i++)
    if (!isfinite(m->packet[i]))
      goto damaged;
  enqueue(m, produced);
  if (m->recovering) {
    m->recoveries++;
    m->recovering = 0;
  }
  return 0;
damaged:
  m->errors++;
  m->recovering = 1;
  if (reset_decoder(m) < 0)
    return -3;
  return -2;
}

BH_LDAC_API uint32_t bh_ldac_media_read(BhLdacMedia *m, float *output, uint32_t frames) {
  if (!m || !output)
    return 0;
  if (frames > m->queued)
    frames = m->queued;
  uint32_t first = frames < QUEUE - m->read ? frames : QUEUE - m->read;
  memcpy(output, m->queue + m->read * 2, first * 2 * sizeof(float));
  memcpy(output + first * 2, m->queue, (frames - first) * 2 * sizeof(float));
  m->read = (m->read + frames) % QUEUE;
  m->queued -= frames;
  return frames;
}

BH_LDAC_API uint64_t bh_ldac_media_value(BhLdacMedia *m, int field) {
  if (!m)
    return 0;
  switch (field) {
  case 0:
    return m->frames;
  case 1:
    return m->bytes;
  case 2:
    return m->coded;
  case 3:
    return m->errors;
  case 4:
    return m->recoveries;
  case 5:
    return m->lost;
  case 6:
    return (uint64_t)m->recovering;
  case 7:
    return m->queued;
  case 8:
    return m->dropped;
  case 9:
    return m->malformed;
  default:
    return 0;
  }
}
