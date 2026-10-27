/* Bounded HCI ISO SDU assembly, Bluetooth Core Vol 4, Part E, 5.4.5.
 * SPDX-License-Identifier: Apache-2.0. No codec, clock, allocation or PLC here. */
#pragma once
#include <array>
#include <cstdint>
#include <cstring>

namespace bh {
struct IsoSdu {
  std::array<uint8_t, 4095> bytes{};
  uint32_t generation = 0, timestamp = 0;
  uint16_t handle = 0, sequence = 0, size = 0, gap = 0;
  uint8_t status = 0;
  bool timestamp_present = false, discontinuity = false;
};
struct IsoSduCounters {
  uint64_t completed = 0, erased = 0, malformed = 0, abandoned = 0;
  uint64_t duplicate = 0, late = 0, stale = 0;
};
class IsoSduAssembler {
  IsoSdu sdu;
  uint16_t handle = 0xffff, limit = 0, at = 0, wanted = 0, previous = 0;
  uint32_t generation = 0;
  bool assembling = false, sequenced = false;
  static uint16_t read16(const uint8_t *p) {
    return static_cast<uint16_t>(p[0] | (static_cast<uint16_t>(p[1]) << 8));
  }
  bool invalid() { ++counters.malformed; assembling = false; at = wanted = 0; return false; }
  void abandon() {
    if (assembling) ++counters.abandoned;
    assembling = false; at = wanted = 0;
  }
  template<class Deliver> void complete(Deliver &deliver) {
    assembling = false;
    if (sequenced) {
      const uint16_t distance = static_cast<uint16_t>(sdu.sequence - previous);
      if (!distance) { ++counters.duplicate; return; }
      if (distance >= 0x8000) { ++counters.late; return; }
      sdu.discontinuity = distance > 256;
      sdu.gap = sdu.discontinuity ? 0 : static_cast<uint16_t>(distance - 1);
    } else { sdu.discontinuity = false; sdu.gap = 0; }
    previous = sdu.sequence; sequenced = true;
    if (sdu.status) { ++counters.erased; sdu.size = 0; }
    ++counters.completed;
    deliver(sdu);
  }
public:
  IsoSduCounters counters;
  bool bind(uint16_t h, uint32_t epoch, uint16_t max_sdu) {
    if (h > 0xeff || !epoch || !max_sdu || max_sdu > sdu.bytes.size()) return false;
    abandon(); handle = h; generation = epoch; limit = max_sdu; sequenced = false;
    sdu = IsoSdu{};
    return true;
  }
  void unbind() { abandon(); handle = 0xffff; generation = 0; sequenced = false; }
  /* The input excludes the H4 indicator. Only the USB owner feeds this object.
   * The callback must copy an SDU before returning if another worker consumes it.
   * Possibly-invalid and lost data both become one erasure, never guessed PCM.
   * Sequence gaps are metadata; only the playout owner requests codec PLC. */
  template<class Deliver>
  bool feed(const uint8_t *p, uint32_t n, uint32_t epoch, Deliver deliver) {
    if (!generation || epoch != generation) { ++counters.stale; return false; }
    if (!p || n < 4) return invalid();
    const uint16_t flags = read16(p), length = read16(p + 2);
    if ((flags & 0xfff) != handle) { ++counters.stale; return false; }
    if ((flags & 0x8000) || (length & 0xc000) || length != n - 4) return invalid();
    const uint8_t boundary = (flags >> 12) & 3;
    const bool timestamp = (flags & 0x4000) != 0;
    if ((boundary & 1) && timestamp) return invalid();
    uint32_t pos = 4;
    if (!(boundary & 1)) {
      if (n < pos + (timestamp ? 8u : 4u)) return invalid();
      abandon();
      sdu.generation = generation; sdu.handle = handle; sdu.timestamp_present = timestamp;
      sdu.timestamp = 0;
      if (timestamp) {
        sdu.timestamp = static_cast<uint32_t>(p[pos]) | (static_cast<uint32_t>(p[pos+1]) << 8) |
                        (static_cast<uint32_t>(p[pos+2]) << 16) | (static_cast<uint32_t>(p[pos+3]) << 24);
        pos += 4;
      }
      sdu.sequence = read16(p + pos);
      const uint16_t info = read16(p + pos + 2);
      pos += 4;
      if (info & 0x3000) return invalid();
      sdu.status = static_cast<uint8_t>(info >> 14);
      if (sdu.status == 3) return invalid();
      wanted = info & 0xfff;
      sdu.size = wanted; at = 0;
      if (wanted > limit) return invalid();
      if (sdu.status == 2 && (boundary != 2 || wanted || n != pos)) return invalid();
      if (boundary == 0 && (sdu.status == 2 || n - pos >= wanted)) return invalid();
      assembling = boundary == 0;
    } else if (!assembling) return invalid();
    const uint32_t fragment = n - pos;
    if (at > wanted || fragment > static_cast<uint32_t>(wanted - at)) return invalid();
    if (fragment) std::memcpy(sdu.bytes.data() + at, p + pos, fragment);
    at = static_cast<uint16_t>(at + fragment);
    if (boundary == 2 || boundary == 3) {
      if (at != wanted) return invalid();
      complete(deliver);
    } else if (at >= wanted) return invalid();
    return true;
  }
};
} // namespace bh
