/* Sole-USB-owner ISO send port. SPDX-License-Identifier: Apache-2.0.
 * The exact same credit/epoch path is used by live USB and offline replay. */
#pragma once
#include "iso_credits.hpp"
#include <cstdint>
namespace bh {
class IsoSender {
  IsoCredits credits;
  uint16_t handle=0xffff;
  uint32_t epoch=0;
public:
  uint64_t invalid=0;
  bool configure(uint16_t length,uint16_t count) {
    if(credits.payload_limit()==length && credits.available()+credits.outstanding()==count) return true;
    if(!count || length<5) return false;
    return credits.configure(length,count);
  }
  bool event(const uint8_t *p,uint32_t n) {return credits.event(p,n);}
  void disconnected(uint16_t h) {
    credits.unbind(h);
    if(handle==h) {handle=0xffff;epoch=0;}
  }
  bool owns(uint16_t h) const {return credits.owns(h);}
  uint16_t available() const {return credits.available();}
  uint16_t outstanding() const {return credits.outstanding();}
  uint64_t completed() const {return credits.completed;}
  template<class Source> uint32_t output(Source &source,uint8_t *p,uint32_t capacity) {
    uint16_t h;uint32_t e;
    bool active=source.iso_transmit_route(h,e);
    if(!active || h!=handle || e!=epoch) {
      handle=0xffff;epoch=0;
      if(!active) return 0;
      if(!credits.payload_limit()) return 0;
      bool bound=credits.owns(h)?credits.rebind(h,e):credits.bind(h,e);
      if(!bound) return 0;
      handle=h;epoch=e;
    }
    if(!credits.available()) return 0;
    uint32_t seen=0;
    auto n=source.iso_output(credits.payload_limit(),p,capacity,seen);
    if(!n) return 0;
    uint16_t current;uint32_t generation;
    if(!source.iso_transmit_route(current,generation) || current!=h || generation!=e) return 0;
    if(n<5 || n>capacity || seen!=e || (uint16_t(p[0] | p[1]<<8)&0xfff)!=h ||
       (p[1]&0x80) || uint32_t(p[2] | p[3]<<8)!=n-4 || n-4>credits.payload_limit()) {invalid++;return 0;}
    if(!credits.take(h,e)) {invalid++;return 0;}
    return n;
  }
};
} // namespace bh
