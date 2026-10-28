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
#include "audio_continuity.h"
#include "audio_float.h"
#include "audio_gap_policy.h"

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
  int have_ssrc, packet_admitted;
  uint32_t gap_fraction, ssrc, pending_damage;
  uint64_t pending_exact, gap_stats[4];
  uint32_t free_source, remaining_source, active_free, active_remaining;
  int context_pending, context_active, pending_fence, drain_uncertain;
  BhDurationProfile duration;
  BhContinuity continuity;
  uint64_t concealed, timeline_resets;
  uint32_t read, queued;
  uint64_t frames, bytes, coded, errors, recoveries, lost, dropped, malformed, accounted_loss;
  float queue[QUEUE * 2], packet[MAX_OUTPUT * 2], decoded[512];
  uint8_t fence[QUEUE];
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
  bhc_reset(&m->continuity);
  bhc_rate(&m->continuity, output_rate);
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
  if (m) {
    m->read = m->queued = 0;
    m->pending_fence = m->drain_uncertain = 0;
    m->context_pending = m->context_active = 0;
  }
}

BH_LDAC_API int bh_ldac_media_restart(BhLdacMedia *m) {
  if (!m)
    return -1;
  m->read = m->queued = m->have_sequence = m->recovering = 0;
  m->have_ssrc = m->packet_admitted = 0;
  m->pending_exact = m->pending_damage = 0;
  m->pending_fence = m->drain_uncertain = 0;
  m->context_pending = m->context_active = 0;
  bhg_reset(&m->duration);
  m->accounted_loss = m->lost;
  m->gap_fraction = 0;
  bhc_reset(&m->continuity);
  bhc_rate(&m->continuity, m->output_rate);
  return reset_decoder(m);
}

static int invalid(BhLdacMedia *m) {
  m->malformed++;
  if (m->packet_admitted && m->pending_damage < 65535u)
    ++m->pending_damage;
  return -1;
}

static int discontinuity(BhLdacMedia *m) {
  if (reset_decoder(m) < 0)
    return -3;
  m->read = m->queued = m->gap_fraction = 0;
  m->recovering = 0;
  m->pending_exact = m->pending_damage = 0;
  m->pending_fence = m->drain_uncertain = 0;
  bhg_reset(&m->duration);
  m->accounted_loss = m->lost;
  bhc_reset(&m->continuity);
  bhc_rate(&m->continuity, m->output_rate);
  ++m->timeline_resets;
  return 0;
}

/* Decoder/SRC output owns the PCM timeline. Only independent missing/damaged
 * SDUs own gaps. Structurally checked damage is exact; absent SDUs are estimates.
 * RTP timestamps have no
 * authority to create audio, reset overlap/SRC state or discard queued PCM. */
static BhGapPlan packet_gap(BhLdacMedia *m, uint32_t current_source_frames) {
  uint64_t lost = m->lost - m->accounted_loss;
  if (!lost && !m->pending_exact && !m->pending_damage) {
    BhGapPlan empty = {0}; return empty;
  }
  BhGapEvidence evidence = {m->pending_exact, lost + m->pending_damage};
  BhGapContext context = {(uint32_t)m->rate, (uint32_t)m->rate/5u,
                          (uint32_t)m->rate/5u, 0, 0, 0};
  if (m->context_active) {
    uint32_t capacity = m->active_free < m->active_remaining ?
                        m->active_free : m->active_remaining;
    capacity = capacity > current_source_frames ? capacity-current_source_frames : 0;
    if (context.exact_budget > capacity) context.exact_budget = capacity;
    if (context.estimated_budget > capacity) context.estimated_budget = capacity;
  }
  return bhg_plan(&m->duration, evidence, context);
}

static void uncertain_boundary(BhLdacMedia *m) {
  m->pending_fence = m->drain_uncertain = 1;
}

static void enqueue(BhLdacMedia *m, const float *data, uint32_t frames, int genuine) {
  if (genuine)
    m->frames += frames;
  else
    m->concealed += frames;
  if (frames > QUEUE) {
    data += (frames - QUEUE) * 2;
    m->dropped += frames - QUEUE;
    frames = QUEUE;
    uncertain_boundary(m);
  }
  if (m->queued + frames > QUEUE) {
    uint32_t skip = m->queued + frames - QUEUE;
    m->read = (m->read + skip) % QUEUE;
    m->queued -= skip;
    m->dropped += skip;
    /* A dropped prefix can contain a recovery fence. Conservatively fence the
     * first retained queued PCM, even if it is synthetic; never silently lose
     * that boundary merely because its original ring slot was overwritten. */
    m->drain_uncertain = 1;
    if (m->queued) m->fence[m->read] = 1;
    else m->pending_fence = 1;
  }
  uint32_t at = (m->read + m->queued) % QUEUE;
  uint32_t first = frames < QUEUE - at ? frames : QUEUE - at;
  memcpy(m->queue + at * 2, data, first * 2 * sizeof(float));
  memcpy(m->queue, data + first * 2, (frames - first) * 2 * sizeof(float));
  memset(m->fence + at, 0, first);
  memset(m->fence, 0, frames-first);
  if (genuine && frames && m->pending_fence) {
    m->fence[at] = 1; m->pending_fence = 0; m->drain_uncertain = 1;
  }
  m->queued += frames;
}

static int media_feed(BhLdacMedia *m, const uint8_t *data, uint32_t size) {
  static const int rates[] = {44100, 48000, 88200, 96000};
  if (!m || !data)
    return -1;
  m->packet_admitted = 0;
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
  uint32_t ssrc =
      ((uint32_t)data[8] << 24) | ((uint32_t)data[9] << 16) | ((uint32_t)data[10] << 8) | data[11];
  int new_stream = !m->have_ssrc || m->ssrc != ssrc;
  uint16_t sequence = (uint16_t)((data[2] << 8) | data[3]);
  if (!new_stream && m->have_sequence) {
    uint16_t distance = (uint16_t)(sequence - m->sequence);
    if (!distance || distance >= 0x8000)
      return 0;
    if (distance > 1)
      m->lost += distance - 1;
  }
  /* A known stream can own a damaged payload and its missing duration. An
   * unvalidated new identity cannot replace that stream or its sequence. */
  if (!new_stream) {
    m->sequence = sequence;
    m->have_sequence = 1;
    m->packet_admitted = 1;
  }
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
  if (new_stream) {
    if (m->have_ssrc && discontinuity(m) < 0)
      return -3;
    m->have_ssrc = m->have_sequence = m->packet_admitted = 1;
    m->ssrc = ssrc;
    m->sequence = sequence;
  }
  uint32_t source_frames = count * (m->rate >= 88200 ? 256u : 128u);
  BhGapPlan gap_plan = packet_gap(m, source_frames);
  /* A deadline/capacity omission is final for this boundary, even when this
   * packet subsequently fails decode. A later packet's fresh budget cannot
   * resurrect an interval already retired here. Keep only still-emittable
   * debt pending until a complete decoded packet can publish it atomically. */
  if (gap_plan.omitted_unknown_sdus || gap_plan.omitted_exact_frames)
    uncertain_boundary(m);
  if (gap_plan.omitted_unknown_sdus) {
    m->gap_stats[2] += gap_plan.omitted_unknown_sdus;
    m->accounted_loss = m->lost; m->pending_damage = 0;
    gap_plan.omitted_unknown_sdus = 0;
  }
  if (gap_plan.omitted_exact_frames) {
    m->gap_stats[3] += gap_plan.omitted_exact_frames;
    m->pending_exact = 0; gap_plan.omitted_exact_frames = 0;
  }
  uint32_t missing = gap_plan.exact_source_frames + gap_plan.estimated_source_frames;
  m->coded += size - 1;
  uint32_t produced = 0;
  for (uint32_t i = 0; i < count; i++) {
    int frames = m->decode(m->decoder, data + positions[i], (int)lengths[i], m->decoded, 512);
    if (frames != (m->rate >= 88200 ? 256 : 128))
      goto damaged;
    for (int sample = 0; sample < frames * 2; sample++)
      if (!isfinite(m->decoded[sample]) || fabsf(m->decoded[sample]) > 16)
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
    if (!isfinite(m->packet[i]) || fabsf(m->packet[i]) > 16)
      goto damaged;
  /* Validate/decode the complete packet before publishing either its gap or
   * PCM. Normal, non-event PCM passes through bhc_real unchanged. */
  if (gap_plan.estimated_source_frames || gap_plan.omitted_unknown_sdus ||
      gap_plan.omitted_exact_frames) uncertain_boundary(m);
  if (missing > 0) {
    uint64_t scaled = (uint64_t)missing * m->output_rate + m->gap_fraction;
    uint32_t gap = (uint32_t)(scaled / m->rate);
    m->gap_fraction = (uint32_t)(scaled % m->rate);
    while (gap) {
      uint32_t take = gap < 256 ? gap : 256;
      for (uint32_t i = 0; i < take; i++)
        bhc_missing(&m->continuity, m->decoded + i * 2);
      enqueue(m, m->decoded, take, 0);
      gap -= take;
    }
    m->recovering = 1;
  }
  for (uint32_t i = 0; i < produced; i++) {
    float merged[2];
    bhc_real(&m->continuity, m->packet + i * 2, merged);
    m->packet[i * 2] = merged[0];
    m->packet[i * 2 + 1] = merged[1];
  }
  enqueue(m, m->packet, produced, 1);
  bhg_observe(&m->duration, source_frames);
  m->gap_stats[0] += gap_plan.exact_source_frames;
  m->gap_stats[1] += gap_plan.estimated_source_frames;
  m->gap_stats[2] += gap_plan.omitted_unknown_sdus;
  m->gap_stats[3] += gap_plan.omitted_exact_frames;
  m->accounted_loss = m->lost;
  m->pending_exact = m->pending_damage = 0;
  if (m->recovering) {
    m->recoveries++;
    m->recovering = 0;
  }
  return 0;
damaged:
  m->errors++;
  /* All frame dimensions passed validation before decode. A failed atomic
   * packet discards count complete frames, not one guessed previous bundle. */
  if (m->context_active && (m->active_free < source_frames ||
                            m->active_remaining < source_frames)) {
    m->gap_stats[3] += source_frames; uncertain_boundary(m);
  } else m->pending_exact += source_frames;
  m->recovering = 1;
  if (reset_decoder(m) < 0)
    return -3;
  return -2;
}

BH_LDAC_API int bh_ldac_media_feed(BhLdacMedia *m, const uint8_t *data, uint32_t size) {
  if (m) {
    m->context_active = m->context_pending;
    m->active_free = m->free_source; m->active_remaining = m->remaining_source;
    m->context_pending = 0;
  }
  unsigned environment = bh_float_enter();
  int result = media_feed(m, data, size);
  bh_float_leave(environment);
  if (m) m->context_active = 0;
  return result;
}

BH_LDAC_API void bh_ldac_media_gap_context(BhLdacMedia *m, uint32_t free_frames,
                                         uint32_t remaining_frames) {
  if (!m) return;
  m->free_source = free_frames; m->remaining_source = remaining_frames;
  m->context_pending = 1;
}
BH_LDAC_API int bh_ldac_media_drain_confidence(BhLdacMedia *m) {
  return m ? m->drain_uncertain : 0;
}
BH_LDAC_API uint32_t bh_ldac_media_read_fenced(BhLdacMedia *m, float *output,
                                            uint8_t *fence, uint32_t frames) {
  if (!m || !output)
    return 0;
  if (frames > m->queued)
    frames = m->queued;
  uint32_t first = frames < QUEUE - m->read ? frames : QUEUE - m->read;
  memcpy(output, m->queue + m->read * 2, first * 2 * sizeof(float));
  memcpy(output + first * 2, m->queue, (frames - first) * 2 * sizeof(float));
  if (fence) {
    memcpy(fence, m->fence+m->read, first);
    memcpy(fence+first, m->fence, frames-first);
  }
  m->read = (m->read + frames) % QUEUE;
  m->queued -= frames;
  if (frames && !m->queued) m->drain_uncertain = 0;
  return frames;
}
BH_LDAC_API uint32_t bh_ldac_media_read(BhLdacMedia *m, float *output, uint32_t frames) {
  return bh_ldac_media_read_fenced(m, output, NULL, frames);
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
  case 10:
    return m->concealed;
  case 11:
    return m->timeline_resets;
  case 20: case 21: case 22: case 23:
    return m->gap_stats[field-20];
  default:
    return 0;
  }
}
