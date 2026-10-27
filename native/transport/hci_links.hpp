/* Controller-side link lifetime. Apache-2.0. No allocation or audio processing.
 * Retire routes BEFORE forwarding events to the signalling process. Classic
 * SCO children use classic peer identity; LE CIS children use their exact ACL
 * parent, and BIS children use their BIG. LE and classic addresses can match. */
#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
namespace bh {
class HciLinks {
  void *acquire_context=nullptr;
  void (*acquire)(void*,uint16_t)=nullptr;
  struct Link {
    uint16_t handle = 0xffff;
    uint8_t mode = 0; // 0: classic ACL, 2: LE ACL; SCO uses its air mode.
    std::array<uint8_t, 6> peer{};
  };
  struct IsoLink { uint16_t handle = 0xffff, parent = 0xffff; uint8_t big = 0xff; };
  std::array<Link, 16> acl;
  std::array<IsoLink, 32> iso;
  static uint16_t u16(const uint8_t *p) { return uint16_t(p[0] | p[1] << 8); }
  bool exists(uint16_t h) const {
    return std::any_of(acl.begin(),acl.end(),[h](const auto &v){return v.handle==h;}) ||
           std::any_of(sco.begin(),sco.end(),[h](const auto &v){return v.handle==h;}) || is_iso(h);
  }
  template<class Retire> void disconnect(uint16_t h, Retire &retire) {
    for (auto &parent : acl) if (parent.handle == h) {
      parent.handle = 0xffff;
      if (parent.mode == 0) for (auto &voice : sco)
        if (voice.handle != 0xffff && voice.peer == parent.peer) {
          auto child = voice.handle; voice.handle = 0xffff; retire(child);
        }
      for (auto &child : iso) if (child.parent == h && child.handle != 0xffff) {
        auto old = child.handle; child = {}; retire(old);
      }
    }
    for (auto &v : sco) if (v.handle == h) v.handle = 0xffff;
    for (auto &v : iso) if (v.handle == h) v = {};
    retire(h);
  }
  template<std::size_t N, class Retire>
  bool remember(std::array<Link,N> &group, uint16_t h, const uint8_t *address, uint8_t mode, Retire &retire) {
    if (h > 0xeff) return false;
    if (exists(h)) disconnect(h,retire);
    auto slot = std::find_if(group.begin(),group.end(),[](const auto &v){return v.handle==0xffff;});
    if (slot == group.end()) return false;
    slot->handle = h; slot->mode = mode; std::copy_n(address,slot->peer.size(),slot->peer.begin());
    if(acquire) acquire(acquire_context,h);
    return true;
  }
  template<class Retire> bool remember_iso(uint16_t h, uint16_t parent, uint8_t big, Retire &retire) {
    if (h > 0xeff || h == parent) return false;
    if (exists(h)) disconnect(h,retire);
    auto slot=std::find_if(iso.begin(),iso.end(),[](const auto &v){return v.handle==0xffff;});
    if(slot==iso.end()) return false;
    *slot={h,parent,big};if(acquire) acquire(acquire_context,h);return true;
  }
public:
  void on_acquire(void *context,void (*callback)(void*,uint16_t)) { acquire_context=context;acquire=callback; }
  std::array<Link,3> sco;
  bool is_iso(uint16_t h) const {
    return std::any_of(iso.begin(),iso.end(),[h](const auto &v){return v.handle==h;});
  }
  template<class Retire> bool event(const uint8_t *p,uint32_t n,Retire retire) {
    if (!p || n<3 || p[1]+2u!=n) return true;
    if (p[0]==0x0e && n>=6 && p[3]==3 && p[4]==0x0c && !p[5]) {
      for (const auto &v:acl) if(v.handle!=0xffff) disconnect(v.handle,retire);
      for (auto &v:sco) if(v.handle!=0xffff) {auto old=v.handle;v.handle=0xffff;retire(old);}
      for (auto &v:iso) if(v.handle!=0xffff) {auto old=v.handle;v={};retire(old);}
      return true;
    }
    if(p[0]==0x0e && n==7 && u16(p+3)==0x206c && !p[5]) {
      // Local BIG Terminate Sync completes without a BIG Sync Lost event.
      for(auto &v:iso) if(v.handle!=0xffff && v.big==p[6]) {auto old=v.handle;v={};retire(old);}
      return true;
    }
    if (p[0]==0x3e) {
      auto sub=p[2];
      if ((sub==1 || sub==0x0a || sub==0x29) && n>=4) {
        uint32_t minimum=sub==1?21:sub==0x0a?33:36;
        if (n<minimum || p[3]) return true;
        return remember(acl,u16(p+4),p+8,2,retire);
      }
      if (sub==0x1a && n==9) {
        auto parent=u16(p+3), handle=u16(p+5);
        auto found=std::find_if(acl.begin(),acl.end(),[parent](const auto &v){return v.handle==parent && v.mode==2;});
        if(found==acl.end()) return false;
        return remember_iso(handle,parent,0xff,retire);
      }
      if (sub==0x19 && n>=6 && p[3]) { // Failed CIS after a previously accepted request.
        auto h=u16(p+4); if(is_iso(h)) disconnect(h,retire); return true;
      }
      if (sub==0x1d && n>=17 && !p[3]) {
        uint8_t big=p[4],count=p[16];
        if (big>0xef || !count || count>31 || n!=17u+2u*count) return false;
        for(uint32_t i=0;i<count;++i) {
          auto h=u16(p+17+2*i);if(h>0xeff) return false;
          for(uint32_t j=0;j<i;++j) if(u16(p+17+2*j)==h) return false;
        }
        for(auto &v:iso) if(v.handle!=0xffff && v.big==big) {auto old=v.handle;v={};retire(old);}
        for(uint32_t i=0;i<count;++i) if(!remember_iso(u16(p+17+2*i),0xffff,big,retire)) return false;
      }
      if ((sub==0x1e || sub==0x1c) && n==5) {
        for(auto &v:iso) if(v.handle!=0xffff && v.big==p[3]) {auto old=v.handle;v={};retire(old);}
      }
      return true;
    }
    if (p[2]) return true;
    if (p[0]==3 && n>=13 && p[11]==1) return remember(acl,u16(p+3),p+5,0,retire);
    if (p[0]==0x2c && n>=19) return remember(sco,u16(p+3),p+5,p[18],retire);
    if (p[0]==5 && n>=6) disconnect(u16(p+3),retire);
    return true;
  }
};
} // namespace bh
