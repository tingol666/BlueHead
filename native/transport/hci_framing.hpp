/* Bluetooth Core Vol 4 E: bounded HCI packets and USB bulk serialization.
 * SPDX-License-Identifier: Apache-2.0. One reader per endpoint/stream. No heap,
 * byte resynchronization guesses or callback allocation on the receive path. */
#pragma once
#include <array>
#include <algorithm>
#include <cstdint>
#include <cstring>
namespace bh {
class HciPacketReader {
  std::array<uint8_t, 65539> data{};
  uint32_t size = 0, wanted = 0;
  uint8_t kind = 0;
  bool broken = false;
  static uint32_t header(uint8_t type) {
    return type == 1 || type == 3 ? 3u : type == 2 || type == 5 ? 4u : type == 4 ? 2u : 0u;
  }
public:
  void reset() { size = wanted = kind = 0; broken = false; }
  bool failed() const { return broken; }
  bool pending() const { return size != 0 || kind != 0; }
  template<class Emit> bool feed(uint8_t type, const uint8_t *p, uint32_t n, Emit emit) {
    // type=0 is H4 bulk serialization; other values are legacy endpoint types.
    if (broken || (!p && n) || type > 5) { broken = true; return false; }
    while (n) {
      if (!kind) {
        kind = type ? type : *p++;
        if (!type) --n;
        if (!header(kind)) { broken = true; return false; }
      } else if (type && kind != type) { broken = true; return false; }
      const uint32_t h = header(kind);
      const uint32_t end = size < h ? h : wanted;
      if (end < size || end > data.size()) { broken = true; return false; }
      const uint32_t take = std::min(n, end - size);
      if (take) std::memcpy(data.data() + size, p, take);
      size += take; p += take; n -= take;
      if (size == h && !wanted) {
        uint32_t length = kind == 1 || kind == 3 ? data[2] : kind == 4 ? data[1] :
                          uint32_t(data[2]) | uint32_t(data[3]) << 8;
        if (kind == 5 && (length & 0xc000)) { broken = true; return false; }
        wanted = h + length;
      }
      if (wanted && size == wanted) {
        if (!emit(kind, data.data(), size)) { broken = true; return false; }
        size = wanted = kind = 0;
      }
    }
    return true;
  }
};
} // namespace bh
