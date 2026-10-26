/* Native port of BlueHead's existing competing-stream hysteresis. Apache-2.0.
 * Counts genuine decode supply, never concealed PCM. Worker-owned state.
 */
#pragma once
#include <cstdint>
namespace bh {
struct StreamGuard {
  bool protected_audio = false, started = false;
  double since = 0;
  uint64_t frames = 0, packets = 0, lost = 0, errors = 0;
  unsigned bad = 0, good = 0;
  void reset() { *this = StreamGuard{}; }
  bool observe(double now, uint64_t decoded, uint64_t received, uint64_t missing, uint64_t failed,
               bool competing, uint32_t pcm_rate = 48000) {
    if (!competing) {
      reset();
      return false;
    }
    if (!started) {
      started = true;
      since = now;
    }
    frames += decoded;
    packets += received;
    lost += missing;
    errors += failed;
    double elapsed = now - since;
    if (elapsed < .5)
      return protected_audio;
    double supply = frames / (pcm_rate * elapsed);
    bool damaged = ((errors >= 3 || lost >= 3) && supply < .8) || (packets >= 10 && supply < .55);
    bool clean = supply >= .9 && !lost && !errors;
    bad = damaged ? bad + 1 : 0;
    good = clean ? good + 1 : 0;
    if (bad >= 2)
      protected_audio = true;
    else if (good >= 3)
      protected_audio = false;
    since = now;
    frames = packets = lost = errors = 0;
    return protected_audio;
  }
};
} // namespace bh
