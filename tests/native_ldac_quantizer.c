/* Test-only oracle: call the pinned AOSP encoder's actual quantizers.
 * Never linked into the application or shipped in release binaries. */
#include "../vendor/libldac_encoder/src/ldaclib.c"

__declspec(dllexport) int bh_test_ldac_quantize(int fine, const float *input,
                                              int *coarse, int *residual) {
  if (fine < 1 || fine > 15 || !input || !coarse || !residual)
    return -1;
  AB block = {0};
  ACSUB samples = {0};
  AC channel = {0};
  block.nqus = 1;
  channel.p_ab = &block;
  channel.p_acsub = &samples;
  channel.a_idwl1[0] = 15;
  channel.a_idwl2[0] = fine;
  memcpy(samples.a_spec, input, 2 * sizeof(float));
  quant_spectrum_ldac(&channel);
  quant_residual_ldac(&channel);
  memcpy(coarse, channel.a_qspec, 2 * sizeof(int));
  memcpy(residual, channel.a_rspec, 2 * sizeof(int));
  return 0;
}
