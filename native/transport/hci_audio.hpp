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
  virtual bool iso_handle(uint16_t) const { return false; }
  virtual bool iso_packet(const uint8_t *, uint32_t) { return false; }
  virtual bool iso_transmit_route(uint16_t &, uint32_t &) const { return false; }
  virtual uint32_t iso_output(uint32_t, uint8_t *, uint32_t, uint32_t &) { return 0; }
  virtual void iso_clock_sample(uint16_t,uint32_t,uint16_t,uint32_t,uint32_t,uint64_t) {}
  virtual void connected(uint16_t) {}
  virtual uint64_t link_identity(uint16_t) const { return 0; }
  virtual uint64_t last_link_identity() const { return 0; }
  virtual void disconnected(uint16_t) = 0;
  virtual void transport_lost() noexcept = 0;
};
inline uint64_t packet_link_identity(const HciTarget &target,uint8_t type,const uint8_t *data,uint32_t size) {
  if(!data) return 0;
  if((type==2 || type==3) && size>=2) return target.link_identity(le16(data)&0xfff);
  if(type==4 && size>=5) {
    if(data[0]==3 || data[0]==0x2c) return target.link_identity(le16(data+3));
    if(data[0]==0x3e && size>=6) {
      if(data[2]==1 || data[2]==0x0a || data[2]==0x29) return target.link_identity(le16(data+4));
      if(data[2]==0x1a && size>=7) return target.link_identity(le16(data+5));
      if(data[2]==0x1d) return target.last_link_identity();
    }
  }
  return 0;
}
} // namespace bh
