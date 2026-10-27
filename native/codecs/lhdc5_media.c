/* RTP -> LHDC V5 -> native-rate float. SPDX-License-Identifier: Apache-2.0.
 * Only an isolated codec process may load this module. No USB, Python, audio
 * endpoint, SRC or integer PCM. This stage alone owns compressed-packet gaps;
 * it never synthesizes audio merely because media was paused/stopped. */
#include "lhdc5_media.h"
#include "audio_continuity.h"
#include "audio_float.h"
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdlib.h>
#include <string.h>
#define BHL_QUEUE 48064u
#define BHL_FRAGMENT 65535u
#define BHL_MAX_FRAMES 32u
struct BhLhdc5Media {
  bh_lhdc5_stream *decoder;
  BhContinuity continuity;
  uint32_t rate, samples, read, queued;
  int have_sequence, have_timestamp, have_ssrc, recovering;
  uint16_t sequence, successful_sequence;
  uint32_t expected_timestamp, previous_timestamp, previous_frames, ssrc;
  uint32_t fragment_size, fragment_timestamp, fragment_ssrc;
  uint8_t fragment_count;
  uint64_t stats[12], accounted_loss, fragment_started;
  uint8_t fragment[BHL_FRAGMENT];
  float queue[BHL_QUEUE * 2], decoded[1920];
};
static uint32_t bhl_be32(const uint8_t *p) { return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3]; }
static void bhl_discontinuity(BhLhdc5Media *m) {
  bh_lhdc5_reset(m->decoder);
  bhc_reset(&m->continuity); bhc_rate(&m->continuity, m->rate);
  m->read = m->queued = 0; m->have_timestamp = 0; m->recovering = 0;
  m->stats[11]++;
}
static int bhl_invalid(BhLhdc5Media *m) {
  m->stats[9]++; m->fragment_size = m->fragment_count = 0;
  return -1;
}
BH_LHDC_API void bh_lhdc5_media_destroy(BhLhdc5Media *m) {
  if (m) { bh_lhdc5_destroy(m->decoder); free(m); }
}
BH_LHDC_API BhLhdc5Media *bh_lhdc5_media_create(int rate, int channels, int depth) {
  if (channels != 2) return NULL;
  BhLhdc5Media *m = calloc(1, sizeof(*m));
  if (!m) return NULL;
  m->decoder = bh_lhdc5_create((uint32_t)rate, (uint32_t)depth);
  if (!m->decoder) { free(m); return NULL; }
  m->rate = (uint32_t)rate; m->samples = bh_lhdc5_samples(m->decoder);
  bhc_reset(&m->continuity); bhc_rate(&m->continuity, m->rate);
  return m;
}
static void bhl_put(BhLhdc5Media *m, const float *pcm, uint32_t n, int real) {
  if (m->queued + n > BHL_QUEUE) {
    uint32_t skip = m->queued + n - BHL_QUEUE;
    m->read = (m->read + skip) % BHL_QUEUE; m->queued -= skip; m->stats[8] += skip;
  }
  for (uint32_t i = 0; i < n; ++i) {
    uint32_t at = (m->read + m->queued) % BHL_QUEUE;
    float *out = m->queue + at * 2;
    if (real) bhc_real(&m->continuity, pcm + i*2, out);
    else bhc_missing(&m->continuity, out);
    ++m->queued;
  }
  m->stats[real ? 0 : 10] += n;
}
static int bhl_feed(BhLhdc5Media *m, const uint8_t *data, uint32_t n) {
  if (!m || !data) return -1;
  m->stats[1] += n;
  if (n < 15 || n > 65535 || data[0] >> 6 != 2) return bhl_invalid(m);
  uint32_t start = 12u + 4u * (data[0] & 15), end = n;
  if (start > end) return bhl_invalid(m);
  if (data[0] & 0x10) {
    if (end - start < 4) return bhl_invalid(m);
    uint32_t words = (uint32_t)data[start+2]<<8 | data[start+3];
    start += 4 + 4*words;
  }
  if (start >= end) return bhl_invalid(m);
  if (data[0] & 0x20) {
    uint32_t pad = data[n-1];
    if (!pad || pad > end - start) return bhl_invalid(m);
    end -= pad;
  }
  if (end - start < 2) return bhl_invalid(m);
  uint16_t sequence = (uint16_t)((uint16_t)data[2]<<8 | data[3]);
  uint32_t timestamp = bhl_be32(data+4), ssrc = bhl_be32(data+8);
  if (m->have_ssrc && m->ssrc != ssrc) {
    bhl_discontinuity(m); m->have_sequence = 0; m->fragment_count = m->fragment_size = 0;
  }
  m->have_ssrc = 1; m->ssrc = ssrc;
  if (m->have_sequence) {
    uint16_t distance = (uint16_t)(sequence - m->sequence);
    if (!distance || distance >= 0x8000) return 0;
    if (distance > 1) {
      m->stats[5] += distance - 1;
      m->fragment_count = m->fragment_size = 0;
    }
  }
  m->have_sequence = 1; m->sequence = sequence;
  uint8_t flags = data[start++];
  const uint8_t *payload = data + start;
  uint32_t bytes = end - start;
  if (flags & 0x80) {
    uint8_t count = flags & 15;
    if (flags & 0x40) {
      if (count < 2 || flags & 0x20) return bhl_invalid(m);
      m->fragment_size = 0; m->fragment_count = count;
      m->fragment_timestamp = timestamp; m->fragment_ssrc = ssrc; m->fragment_started = GetTickCount64();
    } else {
      if (!m->fragment_count || count + 1 != m->fragment_count || !count ||
          ((count == 1) != ((flags & 0x20) != 0)) ||
          timestamp != m->fragment_timestamp || ssrc != m->fragment_ssrc ||
          GetTickCount64() - m->fragment_started > 180) return bhl_invalid(m);
      m->fragment_count = count;
    }
    if (bytes > BHL_FRAGMENT - m->fragment_size) return bhl_invalid(m);
    memcpy(m->fragment + m->fragment_size, payload, bytes); m->fragment_size += bytes;
    if (count > 1) return 0;
    payload = m->fragment; bytes = m->fragment_size;
    m->fragment_size = m->fragment_count = 0;
  } else m->fragment_size = m->fragment_count = 0;
  // LHDC V5 has a sequence/marker byte before self-delimiting frame headers.
  // The generic A2DP nibble above is NOT a trustworthy codec frame count.
  if (bytes < 5) return bhl_invalid(m);
  uint32_t at = 1, count = 0, positions[BHL_MAX_FRAMES], lengths[BHL_MAX_FRAMES];
  while (at < bytes) {
    if (bytes - at < 4 || count == BHL_MAX_FRAMES) return bhl_invalid(m);
    uint32_t size = (((uint32_t)payload[at] | (uint32_t)payload[at+1]<<8) & 1023u)*2u + 2u;
    if (size < 4 || size > bytes - at) return bhl_invalid(m);
    positions[count] = at; lengths[count++] = size; at += size;
  }
  if (!count || at != bytes) return bhl_invalid(m);
  int32_t missing = m->have_timestamp ? (int32_t)(timestamp - m->expected_timestamp) : 0;
  if (m->have_timestamp && timestamp == m->previous_timestamp) {
    uint64_t lost = m->stats[5] - m->accounted_loss;
    if (lost)
      missing = (int32_t)fmin((double)lost * m->previous_frames, m->rate);
    else missing = 0;
  }
  if (missing < 0 || missing > (int32_t)(m->rate/25)) {
    bhl_discontinuity(m); missing = 0;
  }
  if (missing > 0) {
    bh_lhdc5_reset(m->decoder);
    bhl_put(m, NULL, (uint32_t)missing, 0); m->recovering = 1;
  }
  for (uint32_t i=0; i<count; ++i) {
    m->stats[2] += lengths[i];
    int result = bh_lhdc5_decode(m->decoder, payload+positions[i], lengths[i], m->decoded, m->samples);
    if (result != (int)m->samples) {
      m->stats[3]++; m->recovering = 1;
      bhl_put(m, NULL, m->samples, 0);
    } else {
      bhl_put(m, m->decoded, m->samples, 1);
      if (m->recovering) { m->stats[4]++; m->recovering = 0; }
    }
  }
  m->previous_frames = count*m->samples; m->previous_timestamp = timestamp;
  m->expected_timestamp = timestamp + m->previous_frames;
  m->successful_sequence = sequence; m->have_timestamp = 1; m->accounted_loss = m->stats[5];
  return 0;
}
BH_LHDC_API int bh_lhdc5_media_feed(BhLhdc5Media *m, const uint8_t *data, uint32_t n) {
  unsigned state = bh_float_enter(); int result = bhl_feed(m,data,n); bh_float_leave(state); return result;
}
BH_LHDC_API uint32_t bh_lhdc5_media_read(BhLhdc5Media *m, float *out, uint32_t n) {
  if (!m || !out) return 0;
  if (n > m->queued) n = m->queued;
  uint32_t first = n < BHL_QUEUE - m->read ? n : BHL_QUEUE - m->read;
  memcpy(out, m->queue + m->read*2, first*2*sizeof(float));
  memcpy(out+first*2, m->queue, (n-first)*2*sizeof(float));
  m->read = (m->read+n)%BHL_QUEUE; m->queued -= n; return n;
}
BH_LHDC_API uint64_t bh_lhdc5_media_value(BhLhdc5Media *m, int field) {
  if (!m || field<0 || field>=12) return 0;
  return field==6 ? (uint64_t)m->recovering : field==7 ? m->queued : m->stats[field];
}
