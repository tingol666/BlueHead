#pragma once
#include "runtime_common.hpp"
namespace bh {
struct HciTarget {
  virtual ~HciTarget() = default;
  virtual uint32_t media_route(uint16_t handle, uint16_t cid) = 0;
  virtual void media_packet(uint16_t handle, uint16_t cid, uint32_t generation, const uint8_t *,
                            uint32_t) = 0;
  virtual bool voice_packet(uint16_t handle, uint32_t status, const uint8_t *, uint32_t) = 0;
  virtual uint32_t voice_output(uint16_t &handle, uint8_t *, uint32_t capacity) = 0;
  virtual uint32_t voice_byte_rate() const = 0;
  virtual void disconnected(uint16_t) = 0;
  virtual void transport_lost() noexcept = 0;
};
} // namespace bh
