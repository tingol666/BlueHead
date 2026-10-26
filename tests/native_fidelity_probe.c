/* Offline-only access to the unmodified production fractional-delay kernel.
 * Never linked into a product binary; no audio or USB endpoints are opened. */
#include "../native/audio/audio_mixer.c"
API int bh_test_fractional_response(Mixer *m, double normalized_frequency, double fraction,
                                    double *real, double *imaginary) {
  if (!m || !real || !imaginary || normalized_frequency < 0 || normalized_frequency >= .5 ||
      fraction < 0 || fraction > 1)
    return -1;
  Input *s = &m->input[0];
  s->cursor = 0;
  s->available = 128;
  for (int i = 0; i < 128; i++) {
    double angle = 6.2831853071795864769 * normalized_frequency * i;
    s->pcm[i * 2] = (float)cos(angle);
    s->pcm[i * 2 + 1] = (float)sin(angle);
  }
  *real = fraction == 0 ? sample(s, 64, 0) : interpolate(m, s, 64, 0, (float)fraction);
  *imaginary = fraction == 0 ? sample(s, 64, 1) : interpolate(m, s, 64, 1, (float)fraction);
  return 0;
}
