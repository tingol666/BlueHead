/* Bounded HCI ISO fragmentation. Bluetooth Core Vol 4, Part E, 5.4.5.
 * SPDX-License-Identifier: Apache-2.0. Called only by the existing USB owner.
 * Clock deadlines and controller credits are checked by the caller, never by
 * a codec or a Python packet loop. No allocation occurs here. */
#pragma once
#include <array>
#include <algorithm>
#include <cstdint>
#include <cstring>

namespace bh {
class IsoSduFragmenter {
  std::array<uint8_t, 4095> bytes{};
  uint32_t generation = 0, stamp = 0;
  uint16_t handle = 0xffff, sequence = 0, payload_limit = 0, size = 0, offset = 0;
  bool queued = false, timestamp = false;
  static void put16(uint8_t *p, uint16_t value) { p[0] = uint8_t(value); p[1] = uint8_t(value >> 8); }
public:
  uint64_t packets = 0, completed = 0, invalid = 0, stale = 0, abandoned = 0;
  bool bind(uint16_t h, uint32_t epoch, uint16_t max_payload) {
    if (h > 0xeff || !epoch || max_payload < 5 || max_payload > 0x3fff) { ++invalid; return false; }
    cancel(); handle = h; generation = epoch; payload_limit = max_payload;
    return true;
  }
  void cancel() { if (queued) ++abandoned; queued = false; size = offset = 0; }
  void unbind() { cancel(); generation = 0; handle = 0xffff; }
  bool start(uint32_t epoch, uint16_t seq, const uint8_t *data, uint32_t n,
             bool with_timestamp = false, uint32_t timestamp_us = 0) {
    if (!generation || epoch != generation) { ++stale; return false; }
    if (queued || !data || !n || n > bytes.size() ||
        payload_limit <= (with_timestamp ? 8 : 4)) { ++invalid; return false; }
    std::memcpy(bytes.data(), data, n); size = uint16_t(n); offset = 0;
    sequence = seq; stamp = timestamp_us; timestamp = with_timestamp; queued = true;
    return true;
  }
  bool pending() const { return queued; }
  /* Returns an HCI ISO packet WITHOUT the H4 indicator. A short destination or
   * a stale caller never advances the current SDU. Do not interleave two SDUs
   * on one handle; expiration explicitly cancels the old SDU before start(). */
  uint32_t next(uint32_t epoch, uint8_t *out, uint32_t capacity) {
    if (!generation || epoch != generation) { ++stale; return 0; }
    if (!queued) return 0;
    bool first = offset == 0;
    uint32_t overhead = first ? (timestamp ? 8u : 4u) : 0u;
    if (!out || capacity <= 4 + overhead) { ++invalid; return 0; }
    uint32_t room = std::min<uint32_t>(payload_limit, capacity - 4);
    if (room <= overhead) { ++invalid; return 0; }
    uint32_t length = std::min<uint32_t>(size - offset, room - overhead);
    bool last = offset + length == size;
    unsigned boundary = first ? (last ? 2 : 0) : (last ? 3 : 1);
    uint16_t flags = uint16_t(handle | boundary << 12 | (first && timestamp ? 0x4000 : 0));
    put16(out, flags); put16(out + 2, uint16_t(overhead + length));
    uint32_t at = 4;
    if (first) {
      if (timestamp) {
        for (unsigned i = 0; i < 4; ++i) out[at + i] = uint8_t(stamp >> (8 * i));
        at += 4;
      }
      put16(out + at, sequence); put16(out + at + 2, size); at += 4;
    }
    std::memcpy(out + at, bytes.data() + offset, length);
    offset = uint16_t(offset + length); ++packets;
    if (last) { queued = false; ++completed; }
    return at + length;
  }
};
} // namespace bh
