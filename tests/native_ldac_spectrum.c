/* Test-only access to production dequantization, without copied DSP math. */
#include "../vendor/libldacdec/ldacdec.h"
#include "../vendor/libldacdec/spectrum.h"
#include <string.h>

__declspec(dllexport) void bh_test_ldac_dequantize(int fine, const int *coarse,
                                                  const int *residual, float *output) {
  frame_t frame = {0};
  channel_t *channel = &frame.channels[0];
  channel->frame = &frame;
  frame.quantizationUnitCount = 1;
  channel->precisions[0] = 15;
  channel->precisionsFine[0] = fine;
  memcpy(channel->quantizedSpectra, coarse, 2 * sizeof(int));
  memcpy(channel->quantizedSpectraFine, residual, 2 * sizeof(int));
  dequantizeSpectra(channel);
  memcpy(output, channel->spectra, 2 * sizeof(float));
}
