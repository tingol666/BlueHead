/* Control/worker recovery policy. Apache-2.0. No endpoint or USB ownership. */
#pragma once
#include "runtime_common.hpp"
namespace bh {
struct SourceExpired : std::runtime_error {
  SourceExpired() : std::runtime_error("Audio source session has expired") {}
};
enum class Fault { none, endpoint, busy, permission, format, memory, timeout, internal };
inline const char *fault_name(Fault f) {
  switch (f) {
  case Fault::none:
    return "none";
  case Fault::endpoint:
    return "audio_endpoint";
  case Fault::busy:
    return "audio_busy";
  case Fault::permission:
    return "audio_permission";
  case Fault::format:
    return "audio_format";
  case Fault::memory:
    return "audio_memory";
  case Fault::timeout:
    return "audio_timeout";
  default:
    return "audio_internal";
  }
}
inline bool can_retry(Fault f) {
  return f != Fault::permission && f != Fault::format && f != Fault::none;
}
struct AudioFailure : std::runtime_error {
  Fault category;
  int code;
  AudioFailure(Fault kind, int native_code, const char *message)
      : std::runtime_error(message), category(kind), code(native_code) {}
};
/* One audio-control owner. Successful reopen must remain healthy for five
 * seconds before its retry history is forgiven. Manual intent bypasses a
 * cooldown; cancelled/obsolete jobs never call fail(). */
struct Recovery {
  Fault category = Fault::none;
  int code = 0;
  uint32_t attempts = 0;
  uint64_t failures = 0, recovered = 0;
  double retry_at = 0, stable_since = 0;
  bool pending = false, automatic = false, retained = false;
  void manual() {
    category = Fault::none;
    code = 0;
    attempts = 0;
    pending = automatic = retained = false;
    retry_at = stable_since = 0;
  }
  void fail(Fault kind, int value, double now, bool retry) {
    if (category != kind)
      attempts = 0;
    category = kind;
    code = value;
    failures++;
    attempts++;
    pending = true;
    retained = false;
    automatic = retry && can_retry(kind);
    if (kind == Fault::internal && attempts >= 3)
      automatic = false;
    double delay = kind == Fault::memory ? 10 : .5;
    delay *= double(1u << std::min(attempts - 1, 4u));
    retry_at = now + std::min(kind == Fault::memory ? 30.0 : 8.0, delay);
    stable_since = 0;
  }
  bool ready(double now) const { return !pending || (automatic && now >= retry_at); }
  void success(double now) {
    if (pending) {
      recovered++;
      stable_since = now;
    }
    pending = false;
  }
  void keep_previous(double now) {
    pending = false;
    retained = true;
    stable_since = now;
  }
  void healthy(double now, bool active) {
    if (active && !pending && category != Fault::none && stable_since && now - stable_since >= 5) {
      category = Fault::none;
      code = 0;
      attempts = 0;
      retained = false;
      retry_at = stable_since = 0;
    }
  }
  json snapshot(double now) const {
    return {{"category", fault_name(category)},
            {"category_id", static_cast<unsigned>(category)},
            {"native_code", code},
            {"state", pending                   ? (automatic ? "retrying" : "needs_attention")
                      : category == Fault::none ? "healthy"
                      : retained                ? "previous_route_kept"
                                                : "recovering"},
            {"automatic", pending && automatic},
            {"attempts", attempts},
            {"failures", failures},
            {"recoveries", recovered},
            {"retry_in_ms", pending && automatic ? std::max(0.0, (retry_at - now) * 1000) : 0}};
  }
};
/* Decoder errors and malformed wire packets are different. One bad packet
 * must not destroy codec history; only sustained decoder failure rebuilds it. */
struct DecoderRecovery {
  uint32_t consecutive = 0, rebuilds = 0;
  double next = 0, stable = 0;
  bool observe(double now, bool failed, bool malformed, uint64_t real_frames) {
    if (real_frames) {
      consecutive = 0;
      if (!stable)
        stable = now;
      if (now - stable >= 5)
        rebuilds = 0;
      return false;
    }
    if (!failed || malformed)
      return false;
    stable = 0;
    if (++consecutive < 8 || now < next)
      return false;
    consecutive = 0;
    next = now + std::min(8.0, double(1u << std::min(rebuilds++, 3u)));
    return true;
  }
};
} // namespace bh
