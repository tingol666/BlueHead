/* Native bounded A2DP extraction/decoding/SRC. Apache-2.0.
 * FFmpeg's codecs run on the decoder worker, never in an audio callback.
 * Packet input is copied with required FFmpeg padding. All dimensions are checked.
 */
#include "media_engine.h"
#include "audio_continuity.h"
#include "audio_timeline.h"
#include "audio_float.h"
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
void bh_mix_clear(Mixer *, int);
void bh_mix_observe(Mixer *, int, uint64_t, uint64_t);
struct BhMedia {
  int codec, rate, channels, output_rate, have_sequence, syncing;
  uint16_t sequence;
  uint32_t ssrc;
  int have_ssrc;
  int continuity_enabled, have_timestamp, packet_timestamp_valid;
  uint32_t expected_timestamp, packet_timestamp, packet_source_samples, packet_duration_hint,
      gap_fraction;
  uint64_t source_expected, source_decoded, pending_gap_after, missing_samples, timeline_resets;
  uint32_t pending_gap;
  BhPacketClock packet_clock;
  uint64_t packet_position, raw_position;
  int mixer_reset;
  BhContinuity continuity;
  AVCodecContext *context;
  AVCodecParserContext *parser;
  AVFrame *frame;
  AVPacket *packet;
  SwrContext *swr;
  enum AVSampleFormat swr_format;
  AVChannelLayout swr_layout;
  uint8_t fragment[4096], sync[65535 + 192],
      packet_data[MAX_PACKET + AV_INPUT_BUFFER_PADDING_SIZE + 3];
  uint32_t fragment_size, sync_size;
  float queue[QUEUE * 2];
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
  av_parser_close(m->parser);
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
  m->frame = av_frame_alloc();
  m->packet = av_packet_alloc();
  if (codec == 1)
    m->parser = av_parser_init(AV_CODEC_ID_SBC);
  if (!m->frame || !m->packet || (codec == 1 && !m->parser) || open_codec(m) < 0) {
    bh_media_destroy(m);
    return NULL;
  }
  return m;
}
BH_MEDIA_API void bh_media_discard(BhMedia *m) {
  if (m)
    m->read = m->queued = 0;
}
BH_MEDIA_API int bh_media_restart(BhMedia *m) {
  if (!m)
    return -1;
  m->read = m->queued = m->fragment_size = m->sync_size = 0;
  m->syncing = m->have_sequence = 0;
  m->have_timestamp = m->packet_timestamp_valid = m->have_ssrc = 0;
  m->source_expected = m->source_decoded = 0;
  m->pending_gap = m->gap_fraction = 0;
  memset(&m->packet_clock, 0, sizeof(m->packet_clock));
  m->packet_position = m->raw_position = 0;
  m->mixer_reset = 1;
  bhc_reset(&m->continuity);
  reset_swr(m);
  if (m->parser) {
    av_parser_close(m->parser);
    m->parser = av_parser_init(AV_CODEC_ID_SBC);
    if (!m->parser)
      return -1;
  }
  if (m->codec == 2) {
    /* Clear the old synthesis overlap, retaining StreamMuxConfig for
     * useSameStreamMux packets. Reopening would lose that configuration. */
    avcodec_flush_buffers(m->context);
    return 0;
  }
  return open_codec(m);
}
static void enqueue(BhMedia *m, const float *data, uint32_t frames) {
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
  uint32_t at = (m->read + m->queued) % QUEUE, first = frames < QUEUE - at ? frames : QUEUE - at;
  memcpy(m->queue + at * 2, data, first * 2 * sizeof(float));
  memcpy(m->queue, data + first * 2, (frames - first) * 2 * sizeof(float));
  m->queued += frames;
}
BH_MEDIA_API int bh_media_continuity(BhMedia *m, int enabled) {
  if (!m)
    return -1;
  m->continuity_enabled = !!enabled;
  bhc_reset(&m->continuity);
  m->have_timestamp = 0;
  m->source_expected = m->source_decoded = 0;
  m->pending_gap = 0;
  return 0;
}
static void emit_gap(BhMedia *m) {
  if (!m->pending_gap)
    return;
  uint32_t frames = m->pending_gap;
  m->pending_gap = 0;
  if (frames > QUEUE)
    frames = QUEUE;
  for (uint32_t i = 0; i < frames; i++)
    bhc_missing(&m->continuity, m->converted + i * 2);
  enqueue(m, m->converted, frames);
  m->frames -= frames; /* Concealment is never reported as genuine decoded PCM. */
  m->missing_samples += frames;
}
static void timestamp_gap(BhMedia *m, uint32_t timestamp) {
  m->packet_position = bht_packet(&m->packet_clock, timestamp, m->rate);
  m->packet_timestamp = timestamp;
  m->packet_timestamp_valid = 1;
  if (!m->continuity_enabled || !m->have_timestamp)
    return;
  int32_t missing = (int32_t)(timestamp - m->expected_timestamp);
  /* Large seeks are stream discontinuities, not thousands of lost packets.
   * A bounded short continuation is enough; the renderer recovers separately. */
  if (missing > m->rate / 5 || missing < -m->rate / 5) {
    /* A new packet sequence can legitimately seek backwards too. Reusing the
     * old unwrap/parser/SRC state would freeze the source clock or emit audio
     * retained from before the seek. Restart keeps LATM StreamMuxConfig. */
    bh_media_restart(m);
    m->timeline_resets++;
    m->packet_timestamp = timestamp;
    m->packet_timestamp_valid = 1;
    m->packet_position = bht_packet(&m->packet_clock, timestamp, m->rate);
    return;
  }
  if (missing <= 0)
    return;
  uint64_t scaled = (uint64_t)(uint32_t)missing * m->output_rate + m->gap_fraction;
  uint32_t frames = (uint32_t)(scaled / m->rate);
  m->gap_fraction = (uint32_t)(scaled % m->rate);
  /* The waveform predictor fades within 20ms; the missing *timeline* must
   * still keep its full duration. A normal 1024-sample AAC/aptX HD packet is
   * 21.33ms at 48kHz. Capping that duration to 20ms silently loses 64 frames
   * per packet loss and eventually exhausts the render queue. bhc_missing()
   * emits silence beyond its finite prediction budget instead of stretching
   * fabricated audio indefinitely. Large seeks were handled above. */
  m->pending_gap += frames;
  if (m->pending_gap > (uint32_t)m->output_rate / 5)
    m->pending_gap = (uint32_t)m->output_rate / 5;
  /* Some parsers still hold the previous good frame. Insert the gap only
   * after that frame, before publishing the next frame on the timeline. */
  m->pending_gap_after = m->source_expected;
  if (m->source_decoded >= m->pending_gap_after)
    emit_gap(m);
}
static int frame_pcm(BhMedia *m) {
  AVFrame *f = m->frame;
  if (f->sample_rate != m->rate || f->nb_samples < 0 || f->nb_samples > 32768 ||
      f->ch_layout.nb_channels < 1 || f->ch_layout.nb_channels > 2)
    return -1;
  float *output = m->converted;
  int frames = -1;
  enum AVSampleFormat packed = av_get_packed_sample_fmt(f->format);
  if (m->output_rate == m->rate && f->ch_layout.nb_channels == 2 &&
      (packed == AV_SAMPLE_FMT_FLT || packed == AV_SAMPLE_FMT_S16 || packed == AV_SAMPLE_FMT_S32)) {
    /* The decoder already runs on the graph rate. Format conversion needs
     * neither a resampler nor integer re-quantization. Preserve stereo and
     * FFmpeg's mono rematrix policy by keeping the mono case on swresample. */
    int planar = av_sample_fmt_is_planar(f->format);
    frames = f->nb_samples;
    for (int i = 0; i < frames; i++)
      for (int ch = 0; ch < 2; ch++) {
        const void *data = f->extended_data[planar ? ch : 0];
        int at = planar ? i : i * 2 + ch;
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
    enqueue(m, output, (uint32_t)frames);
    m->source_decoded += (uint32_t)f->nb_samples;
    m->packet_source_samples += (uint32_t)f->nb_samples;
    if (m->pending_gap && m->source_decoded >= m->pending_gap_after)
      emit_gap(m);
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
  for (int start = words - 16; start >= 0 && tries < 16; start--) {
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
  if (!m->parser) {
    int result = decode(m, data, size);
    if (result < 0)
      m->errors++;
    return result;
  }
  /* A2DP packs at most 15 SBC frames. Bound even a parser that repeatedly
   * emits buffered output without consuming malformed input. */
  unsigned iterations = 0;
  while (size) {
    if (++iterations > 64) {
      m->errors++;
      return -1;
    }
    uint8_t *out = NULL;
    int out_size = 0;
    int consumed = av_parser_parse2(m->parser, m->context, &out, &out_size, data, (int)size,
                                    AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
    if (consumed < 0 || (uint32_t)consumed > size || (!consumed && !out_size)) {
      m->errors++;
      return -1;
    }
    if (out_size && decode(m, out, (uint32_t)out_size) < 0) {
      m->errors++;
      return -1;
    }
    data += consumed;
    size -= (uint32_t)consumed;
  }
  return 0;
}
static int invalid(BhMedia *m) {
  m->malformed++;
  m->fragment_size = 0;
  return -1;
}
static int feed_packet(BhMedia *m, const uint8_t *data, uint32_t size) {
  if (!m || !data)
    return -1;
  m->bytes += size;
  if (!size || size > MAX_PACKET)
    return invalid(m);
  if (m->codec == 3) {
    if (size % 4)
      return invalid(m);
    m->coded += size;
    m->packet_position = bht_scale(m->raw_position, m->rate, BHT_RATE);
    m->raw_position += size; /* aptX stereo: four coded bytes represent four frames. */
    return chunk(m, data, size);
  }
  if (size < 12 || data[0] >> 6 != 2)
    return invalid(m);
  uint32_t offset = 12 + 4 * (data[0] & 15), end = size;
  if (offset > size)
    return invalid(m);
  if (data[0] & 0x10) {
    if (offset + 4 > size)
      return invalid(m);
    offset += 4 + 4 * (((uint32_t)data[offset + 2] << 8) | data[offset + 3]);
  }
  if (data[0] & 0x20) {
    uint32_t padding = data[size - 1];
    if (!padding || offset > size || padding > size - offset)
      return invalid(m);
    end -= padding;
  }
  if (offset >= end)
    return invalid(m);
  uint32_t ssrc =
      ((uint32_t)data[8] << 24) | ((uint32_t)data[9] << 16) | ((uint32_t)data[10] << 8) | data[11];
  if (m->have_ssrc && m->ssrc != ssrc) {
    if (bh_media_restart(m) < 0)
      return invalid(m);
    m->timeline_resets++;
  }
  m->ssrc = ssrc;
  m->have_ssrc = 1;
  uint16_t sequence = (uint16_t)((data[2] << 8) | data[3]);
  if (m->have_sequence) {
    uint16_t distance = (uint16_t)(sequence - m->sequence);
    if (!distance || distance > 0x8000)
      return 0;
    if (distance > 1) {
      m->lost += distance - 1;
      m->fragment_size = 0;
    }
  }
  uint32_t timestamp =
      ((uint32_t)data[4] << 24) | ((uint32_t)data[5] << 16) | ((uint32_t)data[6] << 8) | data[7];
  timestamp_gap(m, timestamp);
  m->sequence = sequence;
  m->have_sequence = 1;
  data += offset;
  size = end - offset;
  if (m->codec == 1 && size >= 3 && !(data[0] & 0x80) && (data[0] & 15) && data[1] == 0x9c) {
    m->packet_duration_hint =
        (data[0] & 15) * (4 * (((data[2] >> 4) & 3) + 1)) * ((data[2] & 1) ? 8 : 4);
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
    return chunk(m, data, size);
  }
  uint8_t header = data[0];
  data++;
  size--;
  if (header & 0x80) {
    if (header & 0x40)
      m->fragment_size = 0;
    else if (!m->fragment_size)
      return invalid(m);
    if (size > sizeof(m->fragment) - m->fragment_size)
      return invalid(m);
    memcpy(m->fragment + m->fragment_size, data, size);
    m->fragment_size += size;
    if (!(header & 0x20))
      return 0;
    size = m->fragment_size;
    data = m->fragment;
    m->fragment_size = 0;
  } else if (!(header & 15) || size < 4)
    return invalid(m);
  /* FFmpeg's parser requires input padding even when it produces no packet. */
  memcpy(m->packet_data, data, size);
  memset(m->packet_data + size, 0, AV_INPUT_BUFFER_PADDING_SIZE);
  m->coded += size;
  return chunk(m, m->packet_data, size);
}
static int media_feed(BhMedia *m, const uint8_t *data, uint32_t size) {
  if (!m)
    return -1;
  m->packet_timestamp_valid = 0;
  m->packet_source_samples = m->packet_duration_hint = 0;
  int result = feed_packet(m, data, size);
  if (result >= 0 && m->packet_timestamp_valid) {
    uint32_t duration =
        m->packet_duration_hint ? m->packet_duration_hint : m->packet_source_samples;
    if (duration) {
      m->expected_timestamp = m->packet_timestamp + duration;
      m->have_timestamp = 1;
      m->source_expected += duration;
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
BH_MEDIA_API uint32_t bh_media_drain_mixer(BhMedia *m, void *mixer, int index) {
  if (!m || !mixer || index < 0)
    return 0;
  uint32_t total = m->queued;
  while (m->queued) {
    uint32_t take = m->queued < QUEUE - m->read ? m->queued : QUEUE - m->read;
    bh_mix_write(mixer, index, m->queue + m->read * 2, take);
    m->read = (m->read + take) % QUEUE;
    m->queued -= take;
  }
  return total;
}
/* Producer receipt time and RTP position survive arbitrary decoder stalls.
 * A reset is committed with its PCM, never ahead of a worker's session check. */
BH_MEDIA_API uint32_t bh_media_drain_mixer_at(BhMedia *m, void *mixer, int index,
                                              uint64_t arrival) {
  if (!m || !mixer || index < 0)
    return 0;
  if (m->mixer_reset) {
    bh_mix_clear(mixer, index);
    m->mixer_reset = 0;
  }
  bh_mix_observe(mixer, index, m->packet_position, arrival);
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
  default:
    return 0;
  }
}

/* Called once by the isolated native runtime, before workers are started.
 * Codec errors are counters; stderr I/O cannot block a decoder worker. */
BH_MEDIA_API void bh_media_log_quiet(void) { av_log_set_level(AV_LOG_QUIET); }
