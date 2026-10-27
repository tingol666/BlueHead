/* General LE Audio LC3 codec boundary. SPDX-License-Identifier: Apache-2.0
 * Google liblc3 is Apache-2.0; see vendor/liblc3/LICENSE. */
#include "lc3_stream.h"
#include <lc3.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

struct bh_lc3_stream {
  uint32_t rate, duration, channels, octets, samples, directions, workspace_bytes;
  void *encoder_memory[2], *decoder_memory[2];
  lc3_encoder_t encoder[2];
  lc3_decoder_t decoder[2];
};
uint32_t bh_lc3_abi(void) { return 1; }
void bh_lc3_destroy(bh_lc3_stream *s) {
  if (!s) return;
  for (unsigned i = 0; i < 2; ++i) {
    free(s->encoder_memory[i]); free(s->decoder_memory[i]);
  }
  free(s);
}
int bh_lc3_reset_rx(bh_lc3_stream *s) {
  if (!s || !(s->directions & BH_LC3_DECODE)) return -1;
  for (uint32_t i = 0; i < s->channels; ++i) {
    s->decoder[i] = lc3_setup_decoder((int)s->duration, (int)s->rate, 0, s->decoder_memory[i]);
    if (!s->decoder[i]) return -1;
  }
  return 0;
}
int bh_lc3_reset_tx(bh_lc3_stream *s) {
  if (!s || !(s->directions & BH_LC3_ENCODE)) return -1;
  for (uint32_t i = 0; i < s->channels; ++i) {
    s->encoder[i] = lc3_setup_encoder((int)s->duration, (int)s->rate, 0, s->encoder_memory[i]);
    if (!s->encoder[i]) return -1;
  }
  return 0;
}
int bh_lc3_reset(bh_lc3_stream *s) {
  if (!s) return -1;
  if ((s->directions & BH_LC3_ENCODE) && bh_lc3_reset_tx(s)) return -1;
  if ((s->directions & BH_LC3_DECODE) && bh_lc3_reset_rx(s)) return -1;
  return 0;
}
bh_lc3_stream *bh_lc3_create(uint32_t rate, uint32_t dt, uint32_t channels, uint32_t octets) {
  return bh_lc3_create_ex(rate, dt, channels, octets, BH_LC3_ENCODE | BH_LC3_DECODE);
}
bh_lc3_stream *bh_lc3_create_ex(uint32_t rate, uint32_t dt, uint32_t channels,
                               uint32_t octets, uint32_t directions) {
  if ((rate != 8000 && rate != 16000 && rate != 24000 && rate != 32000 && rate != 48000) ||
      (dt != 7500 && dt != 10000) || channels < 1 || channels > 2 ||
      octets < LC3_MIN_FRAME_BYTES || octets > LC3_MAX_FRAME_BYTES ||
      !directions || (directions & ~(BH_LC3_ENCODE | BH_LC3_DECODE))) return NULL;
  bh_lc3_stream *s = calloc(1, sizeof(*s));
  if (!s) return NULL;
  s->rate = rate; s->duration = dt; s->channels = channels; s->octets = octets; s->directions = directions;
  s->samples = (uint32_t)lc3_frame_samples((int)dt, (int)rate);
  unsigned es = lc3_encoder_size((int)dt, (int)rate), ds = lc3_decoder_size((int)dt, (int)rate);
  if (!es || !ds || !s->samples) { bh_lc3_destroy(s); return NULL; }
  for (uint32_t i = 0; i < channels; ++i) {
    if (directions & BH_LC3_ENCODE) {
      s->encoder_memory[i] = calloc(1, es); s->workspace_bytes += es;
      if (!s->encoder_memory[i]) { bh_lc3_destroy(s); return NULL; }
    }
    if (directions & BH_LC3_DECODE) {
      s->decoder_memory[i] = calloc(1, ds); s->workspace_bytes += ds;
      if (!s->decoder_memory[i]) { bh_lc3_destroy(s); return NULL; }
    }
  }
  if (bh_lc3_reset(s)) { bh_lc3_destroy(s); return NULL; }
  return s;
}
uint32_t bh_lc3_workspace_bytes(const bh_lc3_stream *s) { return s ? s->workspace_bytes : 0; }
uint32_t bh_lc3_samples(const bh_lc3_stream *s) { return s ? s->samples : 0; }
uint32_t bh_lc3_frame_bytes(const bh_lc3_stream *s) { return s ? s->channels * s->octets : 0; }
int bh_lc3_encode(bh_lc3_stream *s, const float *pcm, uint32_t count, uint8_t *out, uint32_t capacity) {
  if (!s || !(s->directions & BH_LC3_ENCODE) || !pcm || !out || count != s->samples || capacity < s->octets * s->channels) return -1;
  /* Validate the entire block before any channel advances. Non-finite capture
   * data must never poison the encoder history or a later valid stream. */
  for (uint32_t i = 0; i < count * s->channels; ++i)
    if (!isfinite(pcm[i]) || fabsf(pcm[i]) > 1.0f) return -1;
  for (uint32_t i = 0; i < s->channels; ++i)
    if (lc3_encode(s->encoder[i], LC3_PCM_FORMAT_FLOAT, pcm + i, (int)s->channels,
                   (int)s->octets, out + i * s->octets)) return -1;
  return 0;
}
int bh_lc3_reset_rx_channel(bh_lc3_stream *s,uint32_t channel) {
  if(!s || !(s->directions & BH_LC3_DECODE) || channel>=s->channels) return -1;
  s->decoder[channel]=lc3_setup_decoder((int)s->duration,(int)s->rate,0,s->decoder_memory[channel]);
  return s->decoder[channel]?0:-1;
}
int bh_lc3_decode_mask(bh_lc3_stream *s,const uint8_t *in,uint32_t bytes,
                        uint32_t valid,uint32_t conceal,float *pcm,uint32_t capacity) {
  if(!s || !(s->directions & BH_LC3_DECODE) || !pcm || capacity<s->samples ||
     (valid & conceal) || ((valid | conceal) & ~((1u<<s->channels)-1u)) ||
     (valid ? (!in || bytes!=s->octets*s->channels) : bytes!=0)) return -1;
  int concealed=0;
  for(uint32_t i=0;i<s->channels;++i) {
    if(!((valid | conceal)&(1u<<i))) {
      for(uint32_t n=0;n<s->samples;++n) pcm[n*s->channels+i]=0;
      continue;
    }
    int result=lc3_decode(s->decoder[i],(valid & (1u<<i))?in+i*s->octets:NULL,
                          (int)s->octets,LC3_PCM_FORMAT_FLOAT,pcm+i,(int)s->channels);
    if(result<0) return -1;
    if(result) concealed |= 1<<i;
  }
  return concealed;
}
int bh_lc3_decode(bh_lc3_stream *s,const uint8_t *in,uint32_t bytes,float *pcm,uint32_t capacity) {
  if(!s || (in ? bytes!=s->octets*s->channels : bytes!=0)) return -1;
  uint32_t all=(1u<<s->channels)-1u;
  int result=bh_lc3_decode_mask(s,in,bytes,in?all:0,in?0:all,pcm,capacity);
  return result<0?result:!!result;
}
