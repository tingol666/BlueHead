/* Native ISO controller flow control. Bluetooth Core Vol 4 E 4.1/7.7.19.
 * SPDX-License-Identifier: Apache-2.0. All methods run on the sole USB owner.
 * Credits count HCI DATA PACKETS, including every SDU fragment, never SDUs.
 * No guessed timeout replenishment; connection loss explicitly frees its debt. */
#pragma once
#include <array>
#include <cstdint>
namespace bh {
class IsoCredits {
  struct Link { uint16_t handle = 0xffff, pending = 0; uint32_t epoch = 0; };
  std::array<Link, 16> links{};
  uint16_t total = 0, used = 0, length = 0;
  static uint16_t u16(const uint8_t *p) { return uint16_t(p[0] | p[1] << 8); }
public:
  uint64_t invalid = 0, submitted = 0, completed = 0, retired = 0;
  bool configure(uint16_t packet_length, uint16_t packets) {
    if (used || packet_length < 5 || packet_length > 0x3fff || !packets) { ++invalid; return false; }
    total = packets; length = packet_length; return true;
  }
  bool owns(uint16_t h) const { for (const auto &v : links) if (v.handle == h) return true; return false; }
  bool bind(uint16_t h, uint32_t epoch) {
    if (h > 0xeff || !epoch || !total || owns(h)) { ++invalid; return false; }
    for (auto &v : links) if (v.handle == 0xffff) { v = {h, 0, epoch}; return true; }
    ++invalid; return false;
  }
  // An ASE generation can change while its physical CIS remains established.
  // Already submitted packets still occupy controller buffers until completion.
  bool rebind(uint16_t h,uint32_t epoch) {
    if(!epoch) {++invalid;return false;}
    for(auto &v:links) if(v.handle==h) {v.epoch=epoch;return true;}
    ++invalid;return false;
  }
  bool unbind(uint16_t h) {
    for (auto &v : links) if (v.handle == h) {
      used = uint16_t(used - v.pending); retired += v.pending; v = {}; return true;
    }
    return false;
  }
  bool take(uint16_t h, uint32_t epoch) {
    if (!available()) return false;
    for (auto &v : links) if (v.handle == h && v.epoch == epoch) {
      ++v.pending; ++used; ++submitted; return true;
    }
    ++invalid; return false;
  }
  uint16_t available() const { return uint16_t(total - used); }
  uint16_t payload_limit() const { return length; }
  uint16_t outstanding() const { return used; }
  // A malformed or impossible completion never partially updates the ledger.
  bool event(const uint8_t *p, uint32_t n) {
    if (!p || n < 1 || p[0] != 0x13) return true;
    if (n < 3 || p[1] + 2u != n || n != 3u + 4u * p[2]) { ++invalid; return false; }
    std::array<uint32_t, 16> counts{};
    for (uint32_t at = 3; at < n; at += 4) {
      auto h = u16(p + at), count = u16(p + at + 2);
      if (h > 0xeff) { ++invalid; return false; }
      for (uint32_t i = 0; i < links.size(); ++i) if (links[i].handle == h) counts[i] += count;
    }
    for (uint32_t i = 0; i < links.size(); ++i) if (counts[i] > links[i].pending) { ++invalid; return false; }
    for (uint32_t i = 0; i < links.size(); ++i) {
      links[i].pending = uint16_t(links[i].pending - counts[i]);
      used = uint16_t(used - counts[i]); completed += counts[i];
    }
    return true;
  }
};
} // namespace bh
