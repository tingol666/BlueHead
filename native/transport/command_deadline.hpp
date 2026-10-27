/* Native command response deadline; sole USB/HCI owner only.
 * A Python timeout/cancellation never permits a later opcode's completion to
 * satisfy another command. Deadline is measured where HCI actually arrives,
 * independently of Python/UI scheduling. SPDX-License-Identifier: Apache-2.0. */
#pragma once
#include <cstdint>
namespace bh {
class CommandDeadline {
  uint16_t pending_=0,last_=0;
  uint64_t sent_=0;
  static uint16_t u16(const uint8_t *p) {return uint16_t(p[0]|p[1]<<8);}
public:
  enum class Event {Other,Matched,CreditOnly,Stale,Invalid};
  uint64_t completed=0,stale=0,invalid=0;
  uint16_t opcode() const {return pending_;}
  uint64_t since() const {return sent_;}
  bool sent(uint16_t opcode,uint64_t now) {
    if(opcode==0x0c35) return true; // Host Number Of Completed Packets has no response.
    if(!opcode || pending_) {invalid++;return false;}
    pending_=opcode;sent_=now;return true;
  }
  bool expired(uint64_t now,uint64_t maximum) const {return pending_ && now>=sent_ && now-sent_>=maximum;}
  Event observe(const uint8_t *p,uint32_t n) {
    if(!p || !n || (p[0]!=0x0e && p[0]!=0x0f)) return Event::Other;
    if(n<5 || p[1]+2u!=n || (p[0]==0x0f && n!=6)) {invalid++;return Event::Invalid;}
    auto opcode=p[0]==0x0e?u16(p+3):u16(p+4);
    if(!opcode) return Event::CreditOnly;
    if(pending_==opcode) {last_=opcode;pending_=0;sent_=0;completed++;return Event::Matched;}
    if(last_==opcode || !pending_) {stale++;return Event::Stale;}
    invalid++;return Event::Invalid;
  }
};
} // namespace bh
