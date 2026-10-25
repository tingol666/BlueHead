/* BlueHead bounded float32 bridge. Upstream decoder: MIT, see LICENSE. */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>
#include "ldacdec.h"
_Thread_local jmp_buf ldac_exception;
_Thread_local int ldac_input_length;
__declspec(dllexport) void *bh_ldac_create(void) {
    ldacdec_t *decoder = calloc(1, sizeof(ldacdec_t));
    if (decoder) ldacdecInit(decoder);
    return decoder;
}
__declspec(dllexport) void bh_ldac_free(void *decoder) { free(decoder); }
__declspec(dllexport) int bh_ldac_decode(void *handle, const uint8_t *input,
        int length, float *output, int capacity) {
    if (!handle || !input || !output || length < 4 || length > 515 || capacity < 512)
        return -1;
    uint8_t padded[520] = {0};
    int16_t temporary[512] = {0};
    memcpy(padded, input, length);
    ldac_input_length = length;
    if (setjmp(ldac_exception)) return -2;
    ldacdec_t *decoder = handle;
    int used = 0;
    if (ldacDecode(decoder, padded, temporary, &used) < 0 || used != length) return -3;
    frame_t *frame = &decoder->frame;
    for (int sample = 0; sample < frame->frameSamples; ++sample) {
        for (int channel = 0; channel < 2; ++channel) {
            float value = frame->channels[channel].pcm[sample] / 32768.0f;
            if (!isfinite(value)) return -4;
            output[sample * 2 + channel] = value;
        }
    }
    return frame->frameSamples;
}
