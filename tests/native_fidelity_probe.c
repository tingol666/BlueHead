/* Offline-only access to the production fractional-delay kernel.
 * Never linked into a product binary; no audio or USB endpoints are opened. */
#include "../native/audio/audio_mixer.c"
API int bh_test_fractional_response(Mixer *m, double normalized_frequency, double fraction,
                                    double *real, double *imaginary) {
  if (!m || !real || !imaginary || normalized_frequency < 0 || normalized_frequency >= .5 ||
      fraction < 0 || fraction > 1)
    return -1;
  Input *s = &m->input[0];
  s->cursor = 0;
  s->available = 256;
  for (int i = 0; i < 256; i++) {
    double angle = 6.2831853071795864769 * normalized_frequency * i;
    s->pcm[i * 2] = (float)cos(angle);
    s->pcm[i * 2 + 1] = (float)sin(angle);
  }
  float value[2];
  interpolate_stereo(m->default_route->kernel[0], s, 64, fraction, value);
  *real = value[0];
  *imaginary = value[1];
  return 0;
}
