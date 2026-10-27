/* BlueHead native LHDC V5 adapter. SPDX-License-Identifier: Apache-2.0. */
#include "lhdc5_stream.h"
#include "lhdc_dec.h"
#include "lhdc_imdct.h"
#include <stdatomic.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
static atomic_int occupied;
int g_pkframe, g_nzc_formula;
struct bh_lhdc5_stream {
    void *memory;
    lhdc_decoder_t *decoder;
    uint32_t rate, depth, samples;
    uint64_t decoded, errors, resets;
    float scratch[1920];
};
uint32_t bh_lhdc5_abi(void) { return 1; }
bh_lhdc5_stream *bh_lhdc5_create(uint32_t rate, uint32_t depth) {
    if ((rate != 44100 && rate != 48000 && rate != 96000 && rate != 192000) ||
        (depth != 16 && depth != 24) || atomic_exchange(&occupied, 1)) return NULL;
    bh_lhdc5_stream *s = calloc(1, sizeof(*s));
    if (!s) { atomic_store(&occupied, 0); return NULL; }
    s->rate = rate; s->depth = depth; s->samples = rate == 44100 ? 240 : rate / 200;
    size_t bytes = lhdc_dec_get_workspace_size(rate, 5);
    s->memory = calloc(1, bytes);
    if (s->memory) {
        lhdc_dec_config_t config = {0};
        config.sample_rate = (lhdc_dec_sample_rate_t)rate;
        config.bit_depth = (lhdc_dec_bitdepth_t)depth;
        config.frame_duration = LHDC_DEC_FRAME_5MS;
        config.channels = 2; config.max_frame_bytes = 2048;
        s->decoder = lhdc_dec_init(s->memory, &config);
    }
    if (!s->decoder) { bh_lhdc5_destroy(s); return NULL; }
    return s;
}
void bh_lhdc5_destroy(bh_lhdc5_stream *s) {
    if (!s) return;
    if (s->memory) lhdc_dec_deinit((lhdc_decoder_t *)s->memory);
    lhdc_dec_free_window();
    lhdc_imdct_free_480(); lhdc_imdct_free_960(); lhdc_imdct_free_1920(); lhdc_imdct_free_cos();
    free(s->memory); free(s); atomic_store(&occupied, 0);
}
void bh_lhdc5_reset(bh_lhdc5_stream *s) {
    if (s) { lhdc_dec_reset(s->decoder); s->resets++; }
}
uint32_t bh_lhdc5_samples(const bh_lhdc5_stream *s) { return s ? s->samples : 0; }
int bh_lhdc5_decode(bh_lhdc5_stream *s, const uint8_t *packet, uint32_t bytes, float *pcm, uint32_t capacity) {
    if (!s || !packet || !pcm || bytes < 4 || bytes > 2048 || capacity < s->samples) return -1;
    uint32_t header = (uint32_t)packet[0] | ((uint32_t)packet[1] << 8);
    if ((header & 1023u) * 2u + 2u != bytes) { s->errors++; return -2; }
    size_t consumed = 0;
    uint32_t generated = 0;
    int result = lhdc_dec_decode_frame(s->decoder, packet, bytes, s->scratch, s->samples,
                                      &consumed, &generated, NULL);
    if (result || consumed != bytes || generated != s->samples) {
        s->errors++; lhdc_dec_flush(s->decoder); return -3;
    }
    for (uint32_t i = 0; i < generated * 2; ++i)
        if (!isfinite(s->scratch[i]) || fabsf(s->scratch[i]) > 2.0f) {
            s->errors++; lhdc_dec_flush(s->decoder); return -4;
        }
    memcpy(pcm, s->scratch, generated * 2 * sizeof(float));
    s->decoded += generated;
    return (int)generated;
}
uint64_t bh_lhdc5_value(const bh_lhdc5_stream *s, uint32_t field) {
    if (!s) return 0;
    return field == 0 ? s->decoded : field == 1 ? s->errors : field == 2 ? s->resets : 0;
}
/* From the AOSP forward transform: window repeat N/5; raw synthesis scale
 * (N/2)*2^(bit_depth-31). After normalized float conversion the common scale
 * is N*2^-31. The build adapter uses N/512 in the existing integer-scale
 * expression and removes all subjective per-format gain trims. */
const float *bh_lhdc_window(int n) {
    static float window[1920];
    static int current;
    if (n != 480 && n != 960 && n != 1920) return NULL;
    if (current != n) {
        int repeat = n / 5, zero = (n / 2 - repeat) / 2;
        const double pi = 3.14159265358979323846;
        for (int i = 0; i < n / 2; ++i) {
            double value = 0;
            if (i >= zero + repeat) value = 1;
            else if (i >= zero) {
                double phase = (i - zero + .5) * pi / (2 * repeat);
                double rise = sin(phase);
                value = sin(pi * .5 * rise * rise);
            }
            window[i] = (float)value;
            window[n - 1 - i] = (float)value;
        }
        current = n;
    }
    return window;
}
