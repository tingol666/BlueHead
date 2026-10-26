#pragma once
#include "hci_audio.hpp"
namespace bh {
/* Reassemble only registered media CIDs. Control fragments remain byte exact
 * for Bumble, including interleaving across connection handles. */
class HciDispatch {
  HciTarget &target;
  struct Assembly {
    uint16_t handle = 0xffff, cid = 0;
    uint32_t size = 0, wanted = 0, generation = 0;
    Packet bytes;
  };
  std::unique_ptr<std::array<Assembly, 16>> assemblies =
      std::make_unique<std::array<Assembly, 16>>();

public:
  uint64_t malformed = 0;
  explicit HciDispatch(HciTarget &t) : target(t) {}
  bool acl(const uint8_t *p, uint32_t n) {
    if (n < 4 || le16(p + 2) != n - 4) {
      malformed++;
      return true;
    }
    uint16_t handle = le16(p) & 0xfff;
    unsigned pb = (le16(p) >> 12) & 3;
    Assembly *a = nullptr;
    for (auto &item : *assemblies)
      if (item.handle == handle) {
        a = &item;
        break;
      }
    if (pb != 1) {
      if (a) {
        a->size = a->wanted = 0;
        a->handle = 0xffff;
      }
      if (n < 8) {
        malformed++;
        return true;
      }
      uint16_t cid = le16(p + 6);
      uint32_t generation = target.media_route(handle, cid);
      if (!generation)
        return false;
      for (auto &item : *assemblies)
        if (item.handle == 0xffff) {
          a = &item;
          break;
        }
      if (!a) {
        malformed++;
        return true;
      }
      a->handle = handle;
      a->cid = cid;
      a->generation = generation;
      a->wanted = le16(p + 4);
      a->size = 0;
      p += 8;
      n -= 8;
    } else {
      if (!a)
        return false;
      p += 4;
      n -= 4;
    }
    if (n > a->wanted - a->size) {
      a->handle = 0xffff;
      malformed++;
      return true;
    }
    std::memcpy(a->bytes.data.data() + a->size, p, n);
    a->size += n;
    if (a->size == a->wanted) {
      target.media_packet(handle, a->cid, a->generation, a->bytes.data.data(), a->size);
      a->handle = 0xffff;
    }
    return true;
  }
  void reset(uint16_t handle) {
    for (auto &a : *assemblies)
      if (a.handle == handle)
        a.handle = 0xffff;
  }
};
} // namespace bh
