/* BlueHead native LC3-SWB voice engine. SPDX-License-Identifier: MIT
 * Framing follows Bluetooth HFP 1.9, 6.7.2/6.7.6 and Appendix E.
 * liblc3 is Copyright Google LLC, Apache-2.0; see vendor/liblc3/LICENSE.
 */
#include "voice_engine.h"
#include <lc3.h>
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <speex_resampler.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>

#define H2_BYTES 60u
#define PCM_QUEUE (BH_VOICE_FRAME_SAMPLES * BH_VOICE_QUEUE_FRAMES)
#define MAX_RX_BYTES 4096u
#define CONTEXT_MAGIC 0x42564832u
/* SpanDSP is a separate replaceable LGPL-2.1 DLL, not statically combined
 * with Apache-2.0 liblc3. See vendor/spandsp-plc and tools/build_voice.py. */
typedef struct plc_state_s plc_state_t;
__declspec(dllimport) plc_state_t *plc_init(plc_state_t *state);
__declspec(dllimport) int plc_free(plc_state_t *state);
__declspec(dllimport) int plc_rx(plc_state_t *state, int16_t *pcm, int length);
__declspec(dllimport) int plc_fillin(plc_state_t *state, int16_t *pcm, int length);

struct bh_voice {
    uint32_t magic, codec, ready;
    lc3_encoder_t encoder;
    lc3_decoder_t decoder;
    void *encoder_memory, *decoder_memory;
    AVCodecContext *msbc_encoder, *msbc_decoder;
    AVFrame *msbc_input, *msbc_output;
    AVPacket *msbc_packet;
    SpeexResamplerState *downsample, *upsample;
    uint32_t codec_rate, low_samples;
    int16_t low_pcm[120];
    uint8_t cvsd_low_byte, cvsd_odd, cvsd_damaged;
    uint32_t cvsd_samples;
    plc_state_t *plc;
    uint8_t rx[H2_BYTES], damaged[H2_BYTES], last_wire[H2_BYTES];
    uint32_t rx_size, rx_started, rx_sequence, tx_sequence;
    uint32_t queue_read, queue_size;
    int16_t queue[PCM_QUEUE], decoded[BH_VOICE_FRAME_SAMPLES];
    bh_voice_stats stats;
};

static int valid(const bh_voice *v) { return v && v->magic == CONTEXT_MAGIC; }
static int ready(const bh_voice *v) { return valid(v) && v->ready; }
static int header_valid(const uint8_t *p) {
    return p[0] == 1 && (p[1] == 0x08 || p[1] == 0x38 || p[1] == 0xc8 || p[1] == 0xf8);
}
static uint32_t header_sequence(const uint8_t *p) {
    return ((p[1] >> 4) & 1u) | ((p[1] >> 6) & 2u);
}
static void plc_good(bh_voice *v) {
    plc_rx(v->plc, v->low_pcm, (int)v->low_samples);
}
static void plc_fill(bh_voice *v) {
    plc_fillin(v->plc, v->low_pcm, (int)v->low_samples);
}
static int upsample_frame(bh_voice *v) {
    spx_uint32_t in = v->low_samples, out = BH_VOICE_FRAME_SAMPLES;
    int result = speex_resampler_process_int(v->upsample, 0, v->low_pcm, &in, v->decoded, &out);
    return result || in != v->low_samples || out != BH_VOICE_FRAME_SAMPLES ? -3 : 0;
}
static void queue_pcm(bh_voice *v) {
    uint32_t i;
    if (v->queue_size + BH_VOICE_FRAME_SAMPLES > PCM_QUEUE) {
        uint32_t drop = BH_VOICE_FRAME_SAMPLES;
        v->queue_read = (v->queue_read + drop) % PCM_QUEUE;
        v->queue_size -= drop;
        v->stats.discarded_samples += drop;
        v->stats.queue_overruns++;
    }
    for (i = 0; i < BH_VOICE_FRAME_SAMPLES; ++i)
        v->queue[(v->queue_read + v->queue_size + i) % PCM_QUEUE] = v->decoded[i];
    v->queue_size += BH_VOICE_FRAME_SAMPLES;
    if (v->queue_size > v->stats.high_water_samples) v->stats.high_water_samples = v->queue_size;
}
static int conceal_one(bh_voice *v) {
    if (v->codec == BH_VOICE_LC3_SWB) {
        if (lc3_decode(v->decoder, NULL, 0, LC3_PCM_FORMAT_S16, v->decoded, 1) != 1) return -3;
    } else {
        plc_fill(v);
        if (upsample_frame(v)) return -3;
    }
    queue_pcm(v);
    v->stats.plc_frames++;
    return 1;
}
static int receive_frame(bh_voice *v) {
    uint32_t seq = header_sequence(v->rx), i, missing = 0, damaged = 0;
    int count = 0, result;
    if (v->rx_started) {
        if (seq == v->rx_sequence && !memcmp(v->last_wire, v->rx, H2_BYTES)) {
            v->stats.duplicate_frames++;
            return 0;
        }
        missing = (seq + 4u - ((v->rx_sequence + 1u) % 4u)) % 4u;
        for (i = 0; i < missing; ++i) {
            if (conceal_one(v) < 0) return -3;
            count++;
        }
        v->stats.missing_frames += missing;
    }
    v->rx_started = 1;
    v->rx_sequence = seq;
    memcpy(v->last_wire, v->rx, H2_BYTES);
    for (i = 0; i < H2_BYTES; ++i) damaged |= v->damaged[i];
    if (v->codec == BH_VOICE_LC3_SWB) {
        result = lc3_decode(v->decoder, damaged ? NULL : v->rx + 2, damaged ? 0 : 58,
            LC3_PCM_FORMAT_S16, v->decoded, 1);
    } else {
        result = 1;
        if (!damaged && v->rx[2] == 0xad && !v->rx[3] && !v->rx[4]) {
            uint8_t payload[57 + AV_INPUT_BUFFER_PADDING_SIZE] = {0};
            AVPacket packet = {0};
            memcpy(payload, v->rx + 2, 57);
            packet.data = payload; packet.size = 57;
            av_frame_unref(v->msbc_output);
            if (avcodec_send_packet(v->msbc_decoder, &packet) >= 0
                && avcodec_receive_frame(v->msbc_decoder, v->msbc_output) >= 0
                && v->msbc_output->nb_samples == 120
                && v->msbc_output->format == AV_SAMPLE_FMT_S16P) {
                memcpy(v->low_pcm, v->msbc_output->data[0], 240);
                plc_good(v); result = 0;
            }
        }
        if (result) plc_fill(v);
        if (upsample_frame(v)) return -3;
    }
    if (result < 0) return -3;
    if (result == 1) {
        v->stats.plc_frames++;
        v->stats.corrupt_frames++;
    }
    queue_pcm(v);
    v->stats.rx_frames++;
    return count + 1;
}
uint32_t bh_voice_abi_version(void) { return BH_VOICE_ABI; }
uint32_t bh_voice_codec_available(uint32_t codec_id) {
    return codec_id == BH_VOICE_LC3_SWB || codec_id == BH_VOICE_CVSD
        || (codec_id == BH_VOICE_MSBC && avcodec_find_encoder(AV_CODEC_ID_SBC)
            && avcodec_find_decoder(AV_CODEC_ID_SBC));
}
static int create_msbc(bh_voice *v) {
    AVDictionary *options = NULL;
    int result;
    v->msbc_encoder = avcodec_alloc_context3(avcodec_find_encoder(AV_CODEC_ID_SBC));
    v->msbc_decoder = avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_SBC));
    v->msbc_input = av_frame_alloc(); v->msbc_output = av_frame_alloc(); v->msbc_packet = av_packet_alloc();
    if (!v->msbc_encoder || !v->msbc_decoder || !v->msbc_input || !v->msbc_output || !v->msbc_packet) return -3;
    v->msbc_encoder->sample_rate = 16000;
    v->msbc_encoder->sample_fmt = AV_SAMPLE_FMT_S16;
    av_channel_layout_default(&v->msbc_encoder->ch_layout, 1);
    /* SBC payloads do not carry a sample rate for modified SBC; FFmpeg's
     * decoder needs the HFP negotiated rate supplied by the application. */
    v->msbc_decoder->sample_rate = 16000;
    av_channel_layout_default(&v->msbc_decoder->ch_layout, 1);
    av_dict_set(&options, "msbc", "1", 0);
    result = avcodec_open2(v->msbc_encoder, NULL, &options);
    if (av_dict_count(options) || v->msbc_encoder->frame_size != 120) result = -1;
    av_dict_free(&options);
    if (result < 0 || avcodec_open2(v->msbc_decoder, NULL, NULL) < 0) return -3;
    v->msbc_input->sample_rate = 16000;
    v->msbc_input->format = AV_SAMPLE_FMT_S16;
    v->msbc_input->nb_samples = 120;
    av_channel_layout_default(&v->msbc_input->ch_layout, 1);
    return av_frame_get_buffer(v->msbc_input, 0) < 0 ? -3 : 0;
}
bh_voice *bh_voice_create(uint32_t codec_id) {
    bh_voice *v;
    if (!bh_voice_codec_available(codec_id)) return NULL;
    v = calloc(1, sizeof(*v));
    if (!v) return NULL;
    v->magic = CONTEXT_MAGIC; v->codec = codec_id;
    if (codec_id == BH_VOICE_LC3_SWB) {
        /* The size API takes PCM rate, not the encoded LC3 sample rate. */
        v->encoder_memory = calloc(1, lc3_encoder_size(7500, 48000));
        v->decoder_memory = calloc(1, lc3_decoder_size(7500, 48000));
        if (!v->encoder_memory || !v->decoder_memory) { bh_voice_destroy(v); return NULL; }
    } else {
        int error = 0;
        v->plc = plc_init(NULL);
        if (!v->plc) { bh_voice_destroy(v); return NULL; }
        v->codec_rate = codec_id == BH_VOICE_MSBC ? 16000 : 8000;
        v->low_samples = v->codec_rate * 75u / 10000u;
        v->downsample = speex_resampler_init(1, 48000, v->codec_rate, 5, &error);
        if (!v->downsample || error) { bh_voice_destroy(v); return NULL; }
        v->upsample = speex_resampler_init(1, v->codec_rate, 48000, 5, &error);
        if (!v->upsample || error) {
            bh_voice_destroy(v); return NULL;
        }
    }
    if (bh_voice_reset(v)) {
        bh_voice_destroy(v); return NULL;
    }
    return v;
}
void bh_voice_destroy(bh_voice *v) {
    if (!valid(v)) return;
    v->magic = 0;
    free(v->encoder_memory); free(v->decoder_memory);
    if (v->downsample) speex_resampler_destroy(v->downsample);
    if (v->upsample) speex_resampler_destroy(v->upsample);
    if (v->plc) plc_free(v->plc);
    avcodec_free_context(&v->msbc_encoder); avcodec_free_context(&v->msbc_decoder);
    av_frame_free(&v->msbc_input); av_frame_free(&v->msbc_output); av_packet_free(&v->msbc_packet);
    memset(v, 0, sizeof(*v)); free(v);
}
int bh_voice_reset(bh_voice *v) {
    if (!valid(v)) return -1;
    v->ready = 0;
    if (v->codec == BH_VOICE_LC3_SWB) {
        v->encoder = lc3_setup_encoder(7500, 32000, 48000, v->encoder_memory);
        v->decoder = lc3_setup_decoder(7500, 32000, 48000, v->decoder_memory);
        if (!v->encoder || !v->decoder) return -3;
    } else {
        if (speex_resampler_reset_mem(v->downsample) || speex_resampler_reset_mem(v->upsample)) return -3;
        /* Preserve the SRC's initial zero history: fixed-size codec frames
         * must consume and produce exactly their duration from frame one.
         * skip_zeros would discard initial output and break 7.5ms pacing. */
        /* Encoder flushing is not supported by FFmpeg SBC. Re-open on reset
         * (never on the audio callback) so both analysis and decoder synthesis
         * history reset, including a receive-only context. */
        if (v->codec == BH_VOICE_MSBC) {
            avcodec_free_context(&v->msbc_encoder); avcodec_free_context(&v->msbc_decoder);
            av_frame_free(&v->msbc_input); av_frame_free(&v->msbc_output); av_packet_free(&v->msbc_packet);
            if (create_msbc(v)) return -3;
        }
    }
    if (v->plc) plc_init(v->plc);
    v->cvsd_odd = v->cvsd_damaged = v->cvsd_samples = 0;
    v->rx_size = v->rx_started = v->rx_sequence = v->tx_sequence = 0;
    v->queue_read = v->queue_size = 0;
    memset(v->rx, 0, sizeof(v->rx)); memset(v->damaged, 0, sizeof(v->damaged));
    memset(v->last_wire, 0, sizeof(v->last_wire));
    memset(v->queue, 0, sizeof(v->queue)); memset(v->decoded, 0, sizeof(v->decoded));
    memset(&v->stats, 0, sizeof(v->stats));
    v->stats.abi_version = BH_VOICE_ABI; v->stats.codec_id = v->codec;
    v->ready = 1;
    return 0;
}
int bh_voice_encode(bh_voice *v, const int16_t *pcm, uint32_t samples,
    uint8_t *wire, uint32_t capacity) {
    static const uint8_t sequence[4] = {0x08, 0x38, 0xc8, 0xf8};
    if (!ready(v) || !pcm || !wire || samples != BH_VOICE_FRAME_SAMPLES) return -1;
    if (capacity < (v->codec == BH_VOICE_CVSD ? 120u : H2_BYTES)) return -2;
    if (v->codec == BH_VOICE_LC3_SWB) {
        if (lc3_encode(v->encoder, LC3_PCM_FORMAT_S16, pcm, 1, 58, wire + 2)) return -3;
    } else {
        int16_t low[120];
        spx_uint32_t in = BH_VOICE_FRAME_SAMPLES, out = v->low_samples;
        if (speex_resampler_process_int(v->downsample, 0, pcm, &in, low, &out)
            || in != BH_VOICE_FRAME_SAMPLES || out != v->low_samples) return -3;
        if (v->codec == BH_VOICE_CVSD) {
            uint32_t i;
            for (i = 0; i < v->low_samples; ++i) {
                wire[2 * i] = (uint8_t)low[i]; wire[2 * i + 1] = (uint8_t)((uint16_t)low[i] >> 8);
            }
            v->stats.tx_frames++; return 120;
        }
        if (av_frame_make_writable(v->msbc_input) < 0) return -3;
        memcpy(v->msbc_input->data[0], low, 240);
        av_packet_unref(v->msbc_packet);
        if (avcodec_send_frame(v->msbc_encoder, v->msbc_input) < 0
            || avcodec_receive_packet(v->msbc_encoder, v->msbc_packet) < 0
            || v->msbc_packet->size != 57) return -3;
        memcpy(wire + 2, v->msbc_packet->data, 57); wire[59] = 0;
    }
    wire[0] = 1; wire[1] = sequence[v->tx_sequence++ % 4u];
    v->stats.tx_frames++;
    return H2_BYTES;
}
int bh_voice_push_rx(bh_voice *v, const uint8_t *wire, uint32_t length, uint32_t packet_status) {
    uint32_t i; int produced = 0;
    if (!ready(v) || (!wire && length) || length > MAX_RX_BYTES || packet_status > 3) return -1;
    if (!length) return 0;
    v->stats.rx_packets++; v->stats.rx_bytes += length;
    if (v->codec == BH_VOICE_CVSD) {
        for (i = 0; i < length; ++i) {
            if (!v->cvsd_odd) {
                v->cvsd_low_byte = wire[i]; v->cvsd_odd = 1;
            } else {
                v->low_pcm[v->cvsd_samples++] = (int16_t)((uint16_t)v->cvsd_low_byte | ((uint16_t)wire[i] << 8));
                v->cvsd_odd = 0;
            }
            v->cvsd_damaged |= packet_status != 0;
            if (v->cvsd_samples == v->low_samples) {
                if (v->cvsd_damaged) {
                    if (conceal_one(v) < 0) return -3;
                    v->stats.corrupt_frames++;
                } else {
                    plc_good(v);
                    if (upsample_frame(v)) return -3;
                    queue_pcm(v);
                }
                v->stats.rx_frames++; produced++;
                v->cvsd_samples = v->cvsd_damaged = 0;
            }
        }
        return produced;
    }
    for (i = 0; i < length; ++i) {
        v->rx[v->rx_size] = wire[i];
        v->damaged[v->rx_size++] = (uint8_t)(packet_status != 0);
        if (v->rx_size == 1 && v->rx[0] != 1) {
            v->rx_size = 0; v->stats.resync_bytes++; continue;
        }
        if (v->rx_size == 2 && !header_valid(v->rx)) {
            /* Keep a trailing 0x01: it may begin a split H2 sync word. */
            v->rx[0] = v->rx[1]; v->damaged[0] = v->damaged[1];
            v->rx_size = v->rx[0] == 1 ? 1 : 0;
            v->stats.resync_bytes += 2u - v->rx_size;
            continue;
        }
        if (v->rx_size == H2_BYTES) {
            int result = receive_frame(v);
            v->rx_size = 0;
            if (result < 0) return result;
            produced += result;
        }
    }
    return produced;
}
uint32_t bh_voice_read_pcm(bh_voice *v, int16_t *pcm, uint32_t capacity) {
    uint32_t i, count;
    if (!ready(v) || !pcm) return 0;
    count = capacity < v->queue_size ? capacity : v->queue_size;
    for (i = 0; i < count; ++i) pcm[i] = v->queue[(v->queue_read + i) % PCM_QUEUE];
    v->queue_read = (v->queue_read + count) % PCM_QUEUE; v->queue_size -= count;
    return count;
}
int bh_voice_conceal(bh_voice *v, uint32_t frames) {
    uint32_t i;
    if (!ready(v) || frames > 8) return -1;
    for (i = 0; i < frames; ++i) { if (conceal_one(v) < 0) return -3; }
    /* These frames represent known elapsed slots, so advance the expected H2
     * sequence and do not conceal the same gap when the next packet arrives. */
    if (v->rx_started) v->rx_sequence = (v->rx_sequence + frames) % 4u;
    v->stats.missing_frames += frames;
    return (int)frames;
}
int bh_voice_get_stats(const bh_voice *v, bh_voice_stats *stats, uint32_t stats_size) {
    if (!valid(v) || !stats || stats_size < sizeof(*stats)) return -1;
    *stats = v->stats; stats->queued_samples = v->queue_size;
    return 0;
}
