/* Native bounded A2DP extraction/decoding/SRC. Apache-2.0.
 * FFmpeg's codecs run on the decoder worker, never in an audio callback.
 * Packet input is copied with required FFmpeg padding. All dimensions are checked.
 */
#include "media_engine.h"
#include "audio_continuity.h"
#include "audio_timeline.h"
#include "audio_float.h"
#include "audio_gap_policy.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <limits.h>
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libavutil/log.h>
#include <libswresample/swresample.h>
#define QUEUE 48064u
#define MAX_PACKET 65535u
typedef struct Mixer Mixer;
void bh_mix_write(Mixer *, int, const float *, uint32_t);
void bh_mix_write_fenced(Mixer *, int, const float *, const uint8_t *, uint32_t);
void bh_mix_clear(Mixer *, int);
void bh_mix_observe(Mixer *, int, uint64_t, uint64_t);
void bh_mix_observe_confident(Mixer *, int, uint64_t, uint64_t, uint32_t);
typedef struct {
  uint8_t data[4096];
  uint32_t size, ssrc, timestamp, duration;
  uint16_t sequence;
} SbcAssembly;
struct BhMedia {
  int codec, rate, channels, output_rate, have_sequence, syncing;
  uint16_t sequence;
  uint32_t ssrc;
  int have_ssrc;
  int continuity_enabled, packet_admitted;
  uint32_t packet_lost, packet_source_samples, packet_duration_hint, reserve_source_frames, gap_fraction;
  int defer_aac_gap;
  uint64_t pending_damage, pending_unknown, pending_sbc_unknown_frames, gap_stats[4];
  BhDurationProfile duration, sbc_frame_duration;
  uint64_t missing_samples, timeline_resets;
  uint64_t pending_fragment_damage;
  int sbc_broken_train;
  SbcAssembly sbc_owned, sbc_foreign;
  int mixer_reset;
  BhContinuity continuity;
  AVCodecContext *context;
  AVFrame *frame;
  AVPacket *packet;
  SwrContext *swr;
  enum AVSampleFormat swr_format;
  AVChannelLayout swr_layout;
  uint8_t sync[65535 + 192],
      packet_data[MAX_PACKET + AV_INPUT_BUFFER_PADDING_SIZE + 3];
  uint32_t sync_size;
  float queue[QUEUE * 2];
  uint8_t fence[QUEUE];
  int fence_pending, repair_pending, drain_uncertain, budget_pending, budget_active;
  uint32_t free_pending, remaining_pending, free_active, remaining_active;
  float converted[QUEUE * 2];
  uint32_t read, queued;
  uint64_t frames, bytes, coded, errors, recoveries, lost, dropped, malformed;
  uint64_t direct_frames, converted_frames;
};
static int open_codec(BhMedia *m) {
  const char *names[] = {NULL, "sbc", "aac_latm", "aptx", "aptx_hd"};
  const AVCodec *codec = avcodec_find_decoder_by_name(names[m->codec]);
  if (!codec)
    return -1;
  avcodec_free_context(&m->context);
  m->context = avcodec_alloc_context3(codec);
  if (!m->context)
    return -1;
  m->context->sample_rate = m->rate;
  av_channel_layout_default(&m->context->ch_layout, m->channels);
  m->context->thread_count = 1;
  return avcodec_open2(m->context, codec, NULL);
}
static void reset_swr(BhMedia *m) {
  swr_free(&m->swr);
  av_channel_layout_uninit(&m->swr_layout);
}
BH_MEDIA_API void bh_media_destroy(BhMedia *m) {
  if (!m)
    return;
  reset_swr(m);
  avcodec_free_context(&m->context);
  av_frame_free(&m->frame);
  av_packet_free(&m->packet);
  free(m);
}
BH_MEDIA_API BhMedia *bh_media_create(int codec, int rate, int channels, int output_rate) {
  if (codec < 1 || codec > 4 || (rate != 44100 && rate != 48000) || channels < 1 || channels > 2 ||
      output_rate < 8000 || output_rate > 192000)
    return NULL;
  BhMedia *m = calloc(1, sizeof(*m));
  if (!m)
    return NULL;
  m->codec = codec;
  m->rate = rate;
  m->channels = channels;
  m->output_rate = output_rate;
  bhc_reset(&m->continuity);
  bhc_rate(&m->continuity, m->output_rate);
  m->frame = av_frame_alloc();
  m->packet = av_packet_alloc();
  if (!m->frame || !m->packet || open_codec(m) < 0) {
    bh_media_destroy(m);
    return NULL;
  }
  return m;
}
BH_MEDIA_API void bh_media_discard(BhMedia *m) {
  if (m) {
    m->read = m->queued = 0;
    m->fence_pending = m->repair_pending = m->drain_uncertain = 0;
  }
}
BH_MEDIA_API int bh_media_restart(BhMedia *m) {
  if (!m)
    return -1;
  m->read = m->queued = m->sync_size = 0;
  m->fence_pending = m->repair_pending = m->drain_uncertain = m->budget_pending = m->budget_active = 0;
  m->sbc_owned.size = m->sbc_foreign.size = 0;
  m->pending_fragment_damage = m->pending_sbc_unknown_frames = 0;
  m->sbc_broken_train = 0;
  m->syncing = m->have_sequence = 0;
  m->packet_admitted = m->have_ssrc = 0;
  m->gap_fraction = 0;
  m->pending_unknown = m->packet_lost = m->pending_damage = 0;
  bhg_reset(&m->duration); bhg_reset(&m->sbc_frame_duration);
  m->mixer_reset = 1;
  bhc_reset(&m->continuity);
  bhc_rate(&m->continuity, m->output_rate);
  reset_swr(m);
  if (m->codec == 2) {
    /* Clear the old synthesis overlap, retaining StreamMuxConfig for
     * useSameStreamMux packets. Reopening would lose that configuration. */
    avcodec_flush_buffers(m->context);
    return 0;
  }
  return open_codec(m);
}
BH_MEDIA_API int bh_media_recover_decoder(BhMedia *m) {
  if (!m) return -1;
  /* This is not a source epoch: retain every queued sample and its cursor,
   * cumulative position, valid packet identity/fragments and unchanged SRC.
   * Earlier failed boundaries may already have been omitted; consume only
   * the still-outstanding ledger, never the Runtime failure-streak count. */
  m->gap_stats[2] += m->pending_unknown + m->pending_sbc_unknown_frames;
  m->gap_stats[3] += m->pending_damage + m->pending_fragment_damage;
  m->pending_unknown = m->pending_sbc_unknown_frames = 0;
  m->pending_damage = m->pending_fragment_damage = 0;
  m->packet_lost = m->packet_source_samples = m->packet_duration_hint = 0;
  m->reserve_source_frames = m->defer_aac_gap = m->packet_admitted = 0;
  m->budget_pending = m->budget_active = 0;
  m->sync_size = 0; m->syncing = 0;
  m->fence_pending = m->repair_pending = m->drain_uncertain = 1;
  // Failed synthesis history cannot own another producer-side recovery merge.
  // Consumer-time repair is the sole owner of the new seam.
  uint64_t concealed=m->continuity.concealed, silent=m->continuity.silent,
           merged=m->continuity.merged;
  bhc_reset(&m->continuity); bhc_rate(&m->continuity,m->output_rate);
  m->continuity.concealed=concealed; m->continuity.silent=silent; m->continuity.merged=merged;
  if (m->codec == 2) {
    // AAC-LATM useSameStreamMux needs the retained valid configuration.
    avcodec_flush_buffers(m->context);
    return 0;
  }
  return open_codec(m);
}
static void enqueue(BhMedia *m, const float *data, uint32_t frames, int genuine) {
  m->frames += frames;
  if (!frames) return;
  if (frames > QUEUE) {
    data += (frames - QUEUE) * 2;
    m->dropped += frames - QUEUE;
    frames = QUEUE;
    m->fence_pending = m->drain_uncertain = 1;
  }
  if (m->queued + frames > QUEUE) {
    uint32_t skip = m->queued + frames - QUEUE;
    m->read = (m->read + skip) % QUEUE;
    m->queued -= skip;
    m->dropped += skip;
    m->drain_uncertain = 1;
    if (m->queued) m->fence[m->read] |= BH_PCM_BOUNDARY_UNCERTAIN;
    else m->fence_pending = 1;
  }
  uint32_t at = (m->read + m->queued) % QUEUE, first = frames < QUEUE - at ? frames : QUEUE - at;
  memcpy(m->queue + at * 2, data, first * 2 * sizeof(float));
  memcpy(m->queue, data + first * 2, (frames - first) * 2 * sizeof(float));
  memset(m->fence + at, 0, first);
  memset(m->fence, 0, frames - first);
  if (genuine && m->fence_pending) {
    m->fence[at] = m->repair_pending ? BH_PCM_BOUNDARY_DECODER_REPAIR : BH_PCM_BOUNDARY_UNCERTAIN;
    m->fence_pending = 0;
    m->repair_pending = 0;
    m->drain_uncertain = 1;
  }
  m->queued += frames;
}
BH_MEDIA_API int bh_media_continuity(BhMedia *m, int enabled) {
  if (!m)
    return -1;
  m->continuity_enabled = !!enabled;
  bhc_reset(&m->continuity);
  bhc_rate(&m->continuity, m->output_rate);
  m->pending_fragment_damage = m->pending_sbc_unknown_frames = 0;
  m->pending_unknown = m->packet_lost = m->pending_damage = 0;
  bhg_reset(&m->duration); bhg_reset(&m->sbc_frame_duration);
  return 0;
}
static void emit_gap(BhMedia *m, uint32_t frames) {
  if (!frames) return;
  if (frames > QUEUE)
    frames = QUEUE;
  for (uint32_t i = 0; i < frames; i++)
    bhc_missing(&m->continuity, m->converted + i * 2);
  enqueue(m, m->converted, frames, 0);
  m->frames -= frames; /* Concealment is never reported as genuine decoded PCM. */
  m->missing_samples += frames;
}
static void prepare_gap(BhMedia *m) {
  m->packet_admitted = 1;
  BhGapEvidence evidence = {m->pending_fragment_damage + m->pending_damage,
                            m->pending_unknown + m->packet_lost};
  if (!evidence.exact_source_frames && !evidence.unknown_sdus &&
      !m->pending_sbc_unknown_frames) return;
  BhGapContext context = {(uint32_t)m->rate, (uint32_t)m->rate/5u,
                          (uint32_t)m->rate/5u, 0, 0, 0};
  if (m->budget_active) {
    uint32_t room = m->free_active < m->remaining_active ? m->free_active : m->remaining_active;
    uint32_t reserve = m->reserve_source_frames ? m->reserve_source_frames : m->packet_duration_hint;
    if (!reserve) reserve = bhg_summary(&m->duration).observed_p90;
    room = room > reserve ? room - reserve : 0;
    if (context.exact_budget > room) context.exact_budget = room;
    if (context.estimated_budget > room) context.estimated_budget = room;
  }
  BhGapPlan plan = bhg_plan(&m->duration, evidence, context);
  /* An SBC orphan train has one missing codec frame, not a whole bundled
   * SDU. Its dimensions are estimated separately if its own header is absent. */
  context.exact_budget -= plan.exact_source_frames + plan.estimated_source_frames;
  context.estimated_budget = plan.omitted_exact_frames ? 0 : context.exact_budget;
  BhGapEvidence fragment = {0, m->pending_sbc_unknown_frames};
  BhGapPlan frame_plan = bhg_plan(&m->sbc_frame_duration, fragment, context);
  m->pending_fragment_damage = m->pending_damage = m->pending_unknown = 0;
  m->pending_sbc_unknown_frames = 0;
  if (!m->continuity_enabled) return;
  uint32_t exact = plan.exact_source_frames;
  uint32_t estimated = plan.estimated_source_frames + frame_plan.estimated_source_frames;
  m->gap_stats[0] += exact; m->gap_stats[1] += estimated;
  m->gap_stats[2] += plan.omitted_unknown_sdus + frame_plan.omitted_unknown_sdus;
  m->gap_stats[3] += plan.omitted_exact_frames;
  if (estimated || plan.omitted_unknown_sdus || frame_plan.omitted_unknown_sdus ||
      plan.omitted_exact_frames) m->fence_pending = m->drain_uncertain = 1;
  uint64_t missing = (uint64_t)exact + estimated;
  if (!missing) return;
  /* No estimate can reset the codec or clear genuine queued PCM. Unknown
   * duration evidence is consumed once even when too uncertain/large to emit. */
  uint64_t scaled = missing * m->output_rate + m->gap_fraction;
  uint32_t frames = (uint32_t)(scaled / m->rate);
  m->gap_fraction = (uint32_t)(scaled % m->rate);
  emit_gap(m, frames);
}
static int frame_pcm(BhMedia *m) {
  AVFrame *f = m->frame;
  if (f->sample_rate != m->rate || f->nb_samples < 0 || f->nb_samples > 32768 ||
      f->ch_layout.nb_channels < 1 || f->ch_layout.nb_channels > 2)
    return -1;
  if (m->defer_aac_gap) {
    m->defer_aac_gap = 0;
    /* The pinned LATM callback decodes one AVFrame from our single constructed
     * LOAS packet and has no DELAY capability. Its actual dimensions reserve
     * room before any gap touches PCM/history. A future multi-frame callback
     * requires a new whole-SDU reserve contract, not a guessed percentile. */
    m->reserve_source_frames = (uint32_t)f->nb_samples;
    if (m->budget_active &&
        (m->context->codec->capabilities & AV_CODEC_CAP_DELAY))
      m->free_active = m->remaining_active = 0;
    prepare_gap(m);
  }
  float *output = m->converted;
  int frames = -1;
  enum AVSampleFormat packed = av_get_packed_sample_fmt(f->format);
  if (m->output_rate == m->rate &&
      (f->ch_layout.nb_channels == 1 || f->ch_layout.nb_channels == 2) &&
      (packed == AV_SAMPLE_FMT_FLT || packed == AV_SAMPLE_FMT_S16 || packed == AV_SAMPLE_FMT_S32)) {
    /* Native decoder PCM needs no SRC or re-quantization. Mono is explicitly
     * copied at unity to both output channels; stereo remains independent. */
    int planar = av_sample_fmt_is_planar(f->format);
    frames = f->nb_samples;
    for (int i = 0; i < frames; i++)
      for (int ch = 0; ch < 2; ch++) {
        int source_channel = f->ch_layout.nb_channels == 1 ? 0 : ch;
        const void *data = f->extended_data[planar ? source_channel : 0];
        int at = planar ? i : i * f->ch_layout.nb_channels + source_channel;
        output[i * 2 + ch] = packed == AV_SAMPLE_FMT_FLT ? ((const float *)data)[at]
                             : packed == AV_SAMPLE_FMT_S16
                                 ? ((const int16_t *)data)[at] / 32768.0f
                                 : ((const int32_t *)data)[at] / 2147483648.0f;
      }
    m->direct_frames += frames;
  } else {
    if (!m->swr || f->format != m->swr_format ||
        av_channel_layout_compare(&m->swr_layout, &f->ch_layout)) {
      reset_swr(m);
      AVChannelLayout stereo = AV_CHANNEL_LAYOUT_STEREO;
      if (swr_alloc_set_opts2(&m->swr, &stereo, AV_SAMPLE_FMT_FLT, m->output_rate, &f->ch_layout,
                              f->format, f->sample_rate, 0, NULL) < 0 ||
          swr_init(m->swr) < 0)
        return -1;
      if (av_channel_layout_copy(&m->swr_layout, &f->ch_layout) < 0)
        return -1;
      m->swr_format = f->format;
    }
    int maximum = swr_get_out_samples(m->swr, f->nb_samples);
    if (maximum < 0 || maximum > (int)QUEUE)
      return -1;
    uint8_t *planes[] = {(uint8_t *)output};
    frames =
        swr_convert(m->swr, planes, maximum, (const uint8_t **)f->extended_data, f->nb_samples);
    if (frames > 0)
      m->converted_frames += frames;
  }
  if (frames >= 0) {
    for (int i = 0; i < frames * 2; i++)
      if (!isfinite(output[i]) || fabsf(output[i]) > 16)
        return -1;
    if (m->continuity_enabled)
      for (int i = 0; i < frames; i++) {
        float merged[2];
        bhc_real(&m->continuity, output + i * 2, merged);
        output[i * 2] = merged[0];
        output[i * 2 + 1] = merged[1];
      }
    enqueue(m, output, (uint32_t)frames, 1);
    m->packet_source_samples += (uint32_t)f->nb_samples;
  }
  return frames < 0 ? -1 : 0;
}
static int decode(BhMedia *m, const uint8_t *data, uint32_t size) {
  if (!size || size > MAX_PACKET + 3)
    return -1;
  av_packet_unref(m->packet);
  if (av_new_packet(m->packet, (int)size) < 0)
    return -1;
  memcpy(m->packet->data, data, size);
  int error = avcodec_send_packet(m->context, m->packet);
  if (error < 0)
    return error;
  while ((error = avcodec_receive_frame(m->context, m->frame)) >= 0) {
    int result = frame_pcm(m);
    av_frame_unref(m->frame);
    if (result < 0)
      return result;
  }
  return error == AVERROR(EAGAIN) || error == AVERROR_EOF ? 0 : error;
}
static int parity(const uint8_t *p, int block) {
  return ((p[0] ^ p[block / 2]) >> (block == 6 ? 3 : 5)) & 1;
}
static int aptx_sync(BhMedia *m, const uint8_t *data, uint32_t size) {
  int block = m->codec == 4 ? 6 : 4;
  if (size > sizeof(m->sync) - m->sync_size)
    m->sync_size = 0;
  memcpy(m->sync + m->sync_size, data, size);
  m->sync_size += size;
  int words = (int)m->sync_size / block, tries = 0;
  /* Retain the earliest verified codeword run. Starting at the last window
   * discarded almost an entire intact recovery packet (960 of 1024 samples). */
  for (int start = 0; start <= words - 16 && tries < 16; start++) {
    if (!parity(m->sync + (start + 7) * block, block))
      continue;
    tries++;
    int valid = 1;
    for (int i = 0; i < 16; i++)
      if (parity(m->sync + (start + i) * block, block) != (i == 7 || i == 15)) {
        valid = 0;
        break;
      }
    if (!valid)
      continue;
    if (open_codec(m) < 0)
      return -1;
    reset_swr(m);
    if (decode(m, m->sync + start * block, m->sync_size - (uint32_t)start * block) >= 0) {
      m->sync_size = 0;
      m->syncing = 0;
      m->recoveries++;
      return 0;
    }
  }
  uint32_t keep = m->sync_size < 32u * (uint32_t)block ? m->sync_size : 32u * (uint32_t)block;
  memmove(m->sync, m->sync + m->sync_size - keep, keep);
  m->sync_size = keep;
  return 0;
}
static int chunk(BhMedia *m, const uint8_t *data, uint32_t size) {
  if (m->codec == 3 || m->codec == 4) {
    if (m->syncing)
      return aptx_sync(m, data, size);
    int result = decode(m, data, size);
    if (result < 0) {
      m->errors++;
      m->syncing = 1;
      return aptx_sync(m, data, size);
    }
    return result;
  }
  int result = decode(m, data, size);
  if (result < 0) m->errors++;
  return result;
}
static int invalid(BhMedia *m) {
  m->malformed++;
  return -1;
}
/* Before admission, an unrelated malformed packet owns no stream state. */
static int reject_packet(BhMedia *m) {
  m->malformed++;
  return -1;
}
/* SBC frame dimensions are explicit in the four-byte header. Validate every
 * complete frame before a new SSRC or SDU can retire an existing stream. CRC
 * and coded audio validation still belong to the single FFmpeg decoder. */
static uint32_t sbc_header(BhMedia *m, const uint8_t *data, uint32_t size,
                           uint32_t *samples) {
  static const uint32_t rates[] = {16000, 32000, 44100, 48000};
  if (size < 4 || data[0] != 0x9c)
    return 0;
  uint32_t mode = (data[1] >> 2) & 3, channels = mode ? 2u : 1u;
  uint32_t subbands = (data[1] & 1) ? 8u : 4u;
  uint32_t blocks = 4u * (((data[1] >> 4) & 3u) + 1u), pool = data[2];
  if (rates[data[1] >> 6] != (uint32_t)m->rate || channels != (uint32_t)m->channels ||
      pool < 2 || pool > subbands * (mode < 2 ? 16u : 32u))
    return 0;
  *samples = blocks * subbands;
  uint32_t bits = blocks * pool * (mode < 2 ? channels : 1u) + (mode == 3 ? subbands : 0u);
  return 4u + subbands * channels / 2u + (bits + 7u) / 8u;
}
static int sbc_complete(BhMedia *m, const uint8_t *data, uint32_t size,
                         uint32_t count, uint32_t *duration) {
  uint32_t at = 0, total = 0;
  if (!count || count > 15)
    return 0;
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t samples = 0, length = sbc_header(m, data + at, size - at, &samples);
    if (!length || length > size - at)
      return 0;
    at += length;
    total += samples;
  }
  if (at != size)
    return 0;
  *duration = total;
  return 1;
}
static void sbc_retire(BhMedia *m) {
  if (m->sbc_owned.size) {
    if (m->sbc_owned.duration) m->pending_fragment_damage += m->sbc_owned.duration;
    else ++m->pending_sbc_unknown_frames;
    m->sbc_owned.size = 0;
    m->sbc_broken_train = 1;
  }
}
/* Return a checked complete SBC body, or wait/reject without admitting a new
 * source identity. Foreign fragments have their own bounded staging buffer:
 * their first bytes cannot clear the active decoder, sequence, PCM or assembly. */
static int sbc_payload(BhMedia *m, uint32_t ssrc, uint16_t sequence, uint32_t timestamp,
                         const uint8_t **payload, uint32_t *size, uint32_t *duration) {
  const uint8_t *data = *payload;
  uint8_t flags = data[0];
  data++;
  uint32_t bytes = *size - 1u;
  if (!(flags & 0x80) || (flags & 0xe0) == 0xe0) {
    if (!sbc_complete(m, data, bytes, (flags & 0x80) ? 1u : flags & 15u, duration))
      return reject_packet(m);
    *payload = data;
    *size = bytes;
    return 1;
  }
  /* A first fragment cannot reserve sequence loss or retire an owner before
   * its own immutable size boundary has passed validation. */
  if (bytes > sizeof(m->sbc_owned.data))
    return reject_packet(m);
  int foreign = !m->have_ssrc || m->ssrc != ssrc;
  SbcAssembly *assembly = foreign ? &m->sbc_foreign : &m->sbc_owned;
  uint16_t distance = 1;
  if (foreign && assembly->size && assembly->ssrc == ssrc && (flags & 0x40)) {
    uint16_t candidate_distance = (uint16_t)(sequence - assembly->sequence);
    if (!candidate_distance || candidate_distance >= 0x8000)
      return 0;
  }
  if (!foreign && m->have_sequence) {
    distance = (uint16_t)(sequence - m->sequence);
    if (!distance || distance >= 0x8000)
      return 0;
  }
  if (flags & 0x40) {
    if (!foreign) {
      int old_train = assembly->size || m->sbc_broken_train;
      if (distance > 1) {
        m->lost += distance - 1u;
        /* Before an explicit new first fragment, holes outside a known train
         * are independent missing SDUs. Their unknown duration is bounded by
         * the last complete SDU; never reuse the number as fragment duration. */
        if (!old_train)
          m->pending_unknown += distance - 1u;
      }
      sbc_retire(m);
      m->sbc_broken_train = 0;
    }
    assembly->size = 0;
    assembly->ssrc = ssrc;
    assembly->timestamp = timestamp;
    assembly->duration = 0; /* Only this fragment's own checked header is exact. */
  } else if (!assembly->size || assembly->ssrc != ssrc || assembly->timestamp != timestamp ||
             (uint16_t)(sequence - assembly->sequence) != 1u) {
    if (!foreign) {
      if (distance > 1) {
        m->lost += distance - 1u;
        /* With no received first fragment, an orphan train proves at most
         * one missing SBC frame. Multiple absent middle fragments do not
         * represent multiple codec SDUs. More is unknowable with a fixed RTP
         * clock and no countdown, so keep the estimate conservative. */
        if (!assembly->size && !m->sbc_broken_train)
          ++m->pending_sbc_unknown_frames;
      }
      sbc_retire(m);
      m->sbc_broken_train = !(flags & 0x20);
      /* A rejected orphan tail owns a transport sequence, not a second codec
       * SDU. Its abandoned owner, if known, has already registered the debt. */
      m->sequence = sequence;
      m->have_sequence = 1;
    }
    return reject_packet(m);
  }
  if (!bytes || bytes > sizeof(assembly->data) - assembly->size) {
    if (!foreign)
      sbc_retire(m);
    else
      assembly->size = 0;
    return reject_packet(m);
  }
  memcpy(assembly->data + assembly->size, data, bytes);
  assembly->size += bytes;
  assembly->sequence = sequence;
  uint32_t frame_samples = 0;
  if (sbc_header(m, assembly->data, assembly->size, &frame_samples))
    assembly->duration = frame_samples;
  if (!(flags & 0x20)) {
    if (!foreign) {
      m->sequence = sequence;
      m->have_sequence = 1;
    }
    return 0;
  }
  if (!sbc_complete(m, assembly->data, assembly->size, 1, duration)) {
    if (!foreign) {
      sbc_retire(m);
      m->sequence = sequence;
      m->have_sequence = 1;
    } else
      assembly->size = 0;
    return reject_packet(m);
  }
  *payload = assembly->data;
  *size = assembly->size;
  assembly->size = 0;
  return 1;
}
static int feed_packet(BhMedia *m, const uint8_t *data, uint32_t size) {
  if (!m || !data)
    return -1;
  m->bytes += size;
  if (!size || size > MAX_PACKET)
    return reject_packet(m);
  if (m->codec == 3) {
    if (size % 4)
      return reject_packet(m);
    m->coded += size;
    return chunk(m, data, size);
  }
  if (size < 12 || data[0] >> 6 != 2)
    return reject_packet(m);
  uint32_t offset = 12 + 4 * (data[0] & 15), end = size;
  if (offset > size)
    return reject_packet(m);
  if (data[0] & 0x10) {
    if (offset + 4 > size)
      return reject_packet(m);
    offset += 4 + 4 * (((uint32_t)data[offset + 2] << 8) | data[offset + 3]);
  }
  if (data[0] & 0x20) {
    uint32_t padding = data[size - 1];
    if (!padding || offset > size || padding > size - offset)
      return reject_packet(m);
    end -= padding;
  }
  if (offset >= end)
    return reject_packet(m);
  uint32_t ssrc =
      ((uint32_t)data[8] << 24) | ((uint32_t)data[9] << 16) | ((uint32_t)data[10] << 8) | data[11];
  uint32_t payload_size = end - offset;
  uint16_t sequence = (uint16_t)((data[2] << 8) | data[3]);
  uint32_t timestamp =
      ((uint32_t)data[4] << 24) | ((uint32_t)data[5] << 16) | ((uint32_t)data[6] << 8) | data[7];
  const uint8_t *payload = data + offset;
  uint32_t sbc_duration = 0;
  /* Reject dimensions that cannot contain a codec packet before a new SSRC
   * is allowed to retire the previous decoder and queued PCM. Full
   * codec validation remains the decoder's job; do not trial-decode twice. */
  if ((m->codec == 2 && payload_size > 8191) ||
      (m->codec == 4 && (payload_size < 6 || payload_size % 6)))
    return reject_packet(m);
  if (m->codec == 1) {
    if (payload_size < 2)
      return reject_packet(m);
    int complete = sbc_payload(m, ssrc, sequence, timestamp, &payload, &payload_size, &sbc_duration);
    if (complete <= 0)
      return complete;
  }
  if (m->have_ssrc && m->ssrc != ssrc) {
    int budget = m->budget_active;
    uint32_t free_frames = m->free_active, remaining = m->remaining_active;
    if (bh_media_restart(m) < 0)
      return invalid(m);
    m->budget_active = budget; m->free_active = free_frames; m->remaining_active = remaining;
    m->timeline_resets++;
  }
  m->ssrc = ssrc;
  m->have_ssrc = 1;
  if (m->have_sequence) {
    uint16_t distance = (uint16_t)(sequence - m->sequence);
    if (!distance || distance >= 0x8000)
      return 0;
    if (distance > 1) {
      m->lost += distance - 1;
      m->packet_lost = distance - 1;
    }
  }
  if (m->codec == 1) {
    int old_train = m->sbc_owned.size || m->sbc_broken_train;
    sbc_retire(m);
    if (!old_train)
      m->pending_unknown += m->packet_lost;
    m->packet_lost = 0;
    m->sbc_broken_train = 0;
    m->packet_duration_hint = sbc_duration;
  }
  if (m->codec == 4) m->packet_duration_hint = payload_size / 6u * 4u;
  if (m->codec == 2) {m->packet_admitted = 1; m->defer_aac_gap = 1;}
  else prepare_gap(m);
  m->sequence = sequence;
  m->have_sequence = 1;
  data = payload;
  size = payload_size;
  if (m->codec == 1) {
    /* sbc_complete already established every immutable frame boundary. Feed
     * each frame to the same decoder directly, with its own FFmpeg-required
     * zero padding. No parser can defer a tail frame into the next SDU and
     * misattribute its validation, PCM or duration history. */
    uint32_t at = 0;
    m->coded += size;
    while (at < size) {
      uint32_t samples = 0, length = sbc_header(m, data+at, size-at, &samples);
      if (!length || length > size-at) return invalid(m);
      memcpy(m->packet_data, data+at, length);
      memset(m->packet_data+length, 0, AV_INPUT_BUFFER_PADDING_SIZE);
      if (chunk(m, m->packet_data, length) < 0) return -1;
      bhg_observe(&m->sbc_frame_duration, samples);
      at += length;
    }
    return 0;
  }
  if (m->codec == 2) {
    if (size > 8191)
      return invalid(m);
    m->packet_data[0] = 0x56;
    m->packet_data[1] = (uint8_t)(0xe0 | (size >> 8));
    m->packet_data[2] = (uint8_t)size;
    memcpy(m->packet_data + 3, data, size);
    memset(m->packet_data + size + 3, 0, AV_INPUT_BUFFER_PADDING_SIZE);
    m->coded += size;
    return chunk(m, m->packet_data, size + 3);
  }
  if (m->codec == 4) {
    if (size < 6 || size % 6)
      return invalid(m);
    m->coded += size;
    m->packet_duration_hint = size / 6u * 4u;
    return chunk(m, data, size);
  }
  return reject_packet(m);
}
static int media_feed(BhMedia *m, const uint8_t *data, uint32_t size) {
  if (!m)
    return -1;
  m->packet_admitted = 0;
  m->budget_active = m->budget_pending;
  m->free_active = m->free_pending;
  m->remaining_active = m->remaining_pending;
  m->budget_pending = 0;
  int current_budget = m->budget_active;
  uint32_t current_room = m->free_active < m->remaining_active ? m->free_active : m->remaining_active;
  m->packet_lost = 0;
  m->packet_source_samples = m->packet_duration_hint = 0;
  m->reserve_source_frames = 0; m->defer_aac_gap = 0;
  uint64_t failures = m->errors + m->malformed;
  int result = feed_packet(m, data, size);
  if (m->defer_aac_gap) {
    m->defer_aac_gap = 0;
    /* No valid output dimensions: consume this boundary once without
     * reserving a runtime deadline using guessed current PCM. */
    if (m->budget_active) m->free_active = m->remaining_active = 0;
    prepare_gap(m);
  }
  if (m->packet_admitted) {
    int damaged = result < 0 || m->errors + m->malformed > failures || m->syncing;
    if (damaged) {
      uint32_t nominal = m->packet_duration_hint ? m->packet_duration_hint : m->reserve_source_frames;
      int expired = current_budget && (!current_room || (nominal && current_room < nominal));
      if (m->packet_duration_hint > m->packet_source_samples) {
        uint32_t missing = m->packet_duration_hint - m->packet_source_samples;
        if (expired) {
          m->gap_stats[3] += missing;
          m->fence_pending = m->drain_uncertain = 1;
        } else m->pending_damage += missing;
      } else if (!m->packet_duration_hint && !m->packet_source_samples) {
        if (expired) {
          ++m->gap_stats[2];
          m->fence_pending = m->drain_uncertain = 1;
        } else ++m->pending_unknown;
      }
      else if (!m->packet_duration_hint) {
        ++m->gap_stats[2]; /* Unknown partial remainder, never a second full SDU. */
        m->fence_pending = m->drain_uncertain = 1;
      }
    } else {
      /* SBC/aptX HD supply structurally verified source dimensions. AAC LATM
       * has no parser or delayed codec output (pinned decoder lacks CAP_DELAY),
       * so received source samples belong to this input SDU. Config-only input
       * produces no sample and never trains a guessed duration. */
      uint32_t samples = m->packet_duration_hint ? m->packet_duration_hint :
                         m->packet_source_samples;
      if (m->codec != 3 && (m->codec != 2 ||
          !(m->context->codec->capabilities & AV_CODEC_CAP_DELAY)))
        bhg_observe(&m->duration, samples);
    }
  }
  return result;
}
BH_MEDIA_API int bh_media_feed(BhMedia *m, const uint8_t *data, uint32_t size) {
  unsigned environment = bh_float_enter();
  int result = media_feed(m, data, size);
  bh_float_leave(environment);
  return result;
}
BH_MEDIA_API uint32_t bh_media_read(BhMedia *m, float *output, uint32_t frames) {
  return bh_media_read_fenced(m, output, NULL, frames);
}
BH_MEDIA_API void bh_media_gap_context(BhMedia *m, uint32_t free_frames, uint32_t remaining_frames) {
  if (!m) return;
  m->free_pending = free_frames; m->remaining_pending = remaining_frames; m->budget_pending = 1;
}
BH_MEDIA_API uint32_t bh_media_drain_confidence(BhMedia *m) {
  return m && (m->drain_uncertain || m->fence_pending);
}
BH_MEDIA_API uint32_t bh_media_read_fenced(BhMedia *m, float *output, uint8_t *fence, uint32_t frames) {
  if (!m || !output)
    return 0;
  if (frames > m->queued)
    frames = m->queued;
  uint32_t first = frames < QUEUE - m->read ? frames : QUEUE - m->read;
  memcpy(output, m->queue + m->read * 2, first * 2 * sizeof(float));
  memcpy(output + first * 2, m->queue, (frames - first) * 2 * sizeof(float));
  if (fence) {
    memcpy(fence, m->fence + m->read, first);
    memcpy(fence + first, m->fence, frames - first);
  }
  m->read = (m->read + frames) % QUEUE;
  m->queued -= frames;
  if (!m->queued) m->drain_uncertain = 0;
  return frames;
}
BH_MEDIA_API uint32_t bh_media_drain_mixer(BhMedia *m, void *mixer, int index) {
  if (!m || !mixer || index < 0)
    return 0;
  uint32_t total = m->queued;
  while (m->queued) {
    uint32_t take = m->queued < QUEUE - m->read ? m->queued : QUEUE - m->read;
    bh_mix_write_fenced(mixer, index, m->queue + m->read * 2, m->fence + m->read, take);
    m->read = (m->read + take) % QUEUE;
    m->queued -= take;
  }
  m->drain_uncertain = 0;
  return total;
}
/* Producer receipt time and PCM confidence survive arbitrary decoder stalls.
 * A reset is committed with its PCM, never ahead of a worker's session check. */
BH_MEDIA_API uint32_t bh_media_drain_mixer_at(BhMedia *m, void *mixer, int index,
                                              uint64_t arrival) {
  if (!m || !mixer || index < 0)
    return 0;
  if (m->mixer_reset) {
    bh_mix_clear(mixer, index);
    m->mixer_reset = 0;
  }
  /* Only known intervals measure arrival differences. Estimated or omitted
   * time reanchors confidence, so synthetic duration cannot masquerade as an
   * exact sample clock or contaminate the adaptive jitter budget. */
  if (m->queued) {
    uint64_t supplied = m->frames + m->missing_samples;
    bh_mix_observe_confident(mixer, index, bht_scale(supplied, m->output_rate, BHT_RATE),
                             arrival, bh_media_drain_confidence(m));
  }
  return bh_media_drain_mixer(m, mixer, index);
}
BH_MEDIA_API uint64_t bh_media_value(BhMedia *m, int field) {
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
    return m->syncing;
  case 7:
    return m->queued;
  case 8:
    return m->dropped;
  case 9:
    return m->malformed;
  case 10:
    return m->missing_samples;
  case 11:
    return m->timeline_resets;
  case 12:
    return m->direct_frames;
  case 13:
    return m->converted_frames;
  case 20: case 21: case 22: case 23:
    return m->gap_stats[field-20];
  default:
    return 0;
  }
}

/* Called once by the isolated native runtime, before workers are started.
 * Codec errors are counters; stderr I/O cannot block a decoder worker. */
BH_MEDIA_API void bh_media_log_quiet(void) { av_log_set_level(AV_LOG_QUIET); }
