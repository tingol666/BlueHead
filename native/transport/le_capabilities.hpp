/* LE Audio is gated by transport plus observed HCI capabilities, never a
 * Bluetooth version label. SPDX-License-Identifier: Apache-2.0. */
#pragma once
#include <atomic>
#include <cstdint>
namespace bh {
enum class IsoTransport : uint8_t { None, BulkSerialization, IntelLegacyBulk };
// Standard USB Core Vol 4 B 7; Intel's documented-in-kernel legacy ISO bulk
// path is an explicit vendor backend, never a generic handle-number guess.
inline IsoTransport iso_transport(bool serialized, uint16_t vendor, uint16_t manufacturer) {
  return serialized ? IsoTransport::BulkSerialization :
         vendor==0x8087 && manufacturer==2 ? IsoTransport::IntelLegacyBulk : IsoTransport::None;
}
class LeCapabilities {
  std::atomic<uint64_t> features{0};
  std::atomic<bool> have_features{false}, have_buffers{false};
  std::atomic<uint32_t> iso_length{0}, iso_packets{0};
  std::atomic<IsoTransport> transport{IsoTransport::None};
public:
  void select(IsoTransport value) { transport=value; }
  IsoTransport mode() const { return transport.load(); }
  bool receive() const { return mode()!=IsoTransport::None && have_features && (features & (uint64_t(1)<<29)); }
  bool transmit() const { return receive() && have_buffers && iso_length>=5 && iso_packets!=0; }
  bool broadcast() const { return mode()!=IsoTransport::None && have_features && (features & (uint64_t(1)<<31)); }
  uint16_t packet_length() const { return uint16_t(iso_length.load()); }
  uint16_t packet_count() const { return uint16_t(iso_packets.load()); }
  uint64_t feature_mask() const { return features.load(); }
  bool probed() const { return have_features.load(); }
  void observe(const uint8_t *p,uint32_t n) {
    if (!p || n<6 || p[0]!=0x0e || p[1]+2u!=n || p[5]) return;
    const uint16_t op=uint16_t(p[3] | p[4]<<8);
    if(op==0x0c03) { have_features=false;have_buffers=false;features=0;iso_length=iso_packets=0; }
    if(op==0x2003 && n==14) {
      uint64_t mask=0;for(unsigned i=0;i<8;++i) mask|=uint64_t(p[6+i])<<(8*i);
      features=mask;have_features=true;
    }
    if(op==0x2060 && n==12) {
      uint16_t length=uint16_t(p[9] | p[10]<<8);
      iso_length=length>0x3fff?0x3fff:length;iso_packets=p[11];have_buffers=true;
    }
  }
};
} // namespace bh
