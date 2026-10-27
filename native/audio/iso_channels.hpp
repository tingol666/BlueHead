/* Split-CIS/BIS LC3 channel alignment. SPDX-License-Identifier: Apache-2.0.
 * Worker-only, bounded, no allocation/codec/PLC/USB. Both channels leave on one
 * timeline. Clock mode is explicit; controller timestamps are never guessed
 * from Python scheduling or packet arrival order. */
#pragma once
#include "iso_sdu.hpp"
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
namespace bh {
class IsoChannels {
public:
  enum class Clock {SharedSequence,ControllerTimestamp};
  struct Counters {uint64_t completed=0,partial=0,late=0,duplicate=0,expired=0,malformed=0,unsynchronized=0,resets=0;} counters;
private:
  struct Slot {
    std::array<std::array<uint8_t,800>,2> payload{};
    uint64_t arrival=0;
    uint16_t sequence=0;
    uint8_t present=0,valid=0;
  };
  std::array<Slot,8> slots{};
  uint32_t epoch=0,octets=0,blocks=0,interval_us=0;
  uint64_t grace=0,maximum_age=0,last_arrival=0;
  Clock clock=Clock::ControllerTimestamp;
  bool anchored=false,published=false,restarting=false;
  uint32_t anchor_stamp=0;
  uint16_t anchor_sequence=0,previous=0;
  static bool ahead(uint16_t a,uint16_t b) {auto d=uint16_t(a-b);return d && d<0x8000;}
  void clear() {for(auto &slot:slots)slot.present=slot.valid=0;}
  void restart() {clear();anchored=published=false;restarting=true;counters.resets++;}
public:
  bool configure(uint32_t generation,uint32_t frame_octets,uint32_t frame_blocks,
                 uint32_t sdu_interval_us,uint64_t wait,uint64_t age,Clock mode) {
    if(!generation || frame_octets<20 || frame_octets>400 || frame_blocks<1 || frame_blocks>2 ||
       (sdu_interval_us!=7500*frame_blocks && sdu_interval_us!=10000*frame_blocks) || !age || wait>age) return false;
    clear();epoch=generation;octets=frame_octets;blocks=frame_blocks;interval_us=sdu_interval_us;
    grace=wait;maximum_age=age;clock=mode;last_arrival=0;anchored=published=restarting=false;counters={};return true;
  }
  bool feed(uint8_t lane,const IsoSdu &sdu,uint64_t arrival) {
    if(lane>1 || !epoch || sdu.generation!=epoch ||
       (!sdu.status && sdu.size!=octets*blocks) || sdu.status>2) {counters.malformed++;return false;}
    if(clock==Clock::ControllerTimestamp && !sdu.timestamp_present) {counters.unsynchronized++;return false;}
    if(last_arrival && arrival>=last_arrival && arrival-last_arrival>maximum_age) restart();
    last_arrival=std::max(last_arrival,arrival);
    uint16_t sequence=sdu.sequence;
    if(clock==Clock::ControllerTimestamp) {
      if(!anchored) {anchor_stamp=sdu.timestamp;anchor_sequence=sdu.sequence;anchored=true;}
      int64_t delta=int32_t(sdu.timestamp-anchor_stamp);
      int64_t steps=delta>=0?(delta+interval_us/2)/interval_us:-((-delta+interval_us/2)/interval_us);
      int64_t residual=delta-steps*interval_us;
      if(residual>int64_t(std::max(64u,interval_us/100)) || residual<-int64_t(std::max(64u,interval_us/100))) {
        counters.unsynchronized++;return false;
      }
      sequence=uint16_t(anchor_sequence+steps);
      if(delta>0) {anchor_stamp=sdu.timestamp;anchor_sequence=sequence;}
    }
    if(published && !ahead(sequence,previous)) {counters.late++;return false;}
    auto &slot=slots[sequence%slots.size()];
    if(slot.present && slot.sequence!=sequence) {clear();published=false;restarting=true;counters.resets++;}
    if(!slot.present) {slot.sequence=sequence;slot.arrival=arrival;slot.valid=0;}
    auto bit=uint8_t(1u<<lane);
    if(slot.present&bit) {counters.duplicate++;return false;}
    slot.present|=bit;slot.arrival=std::min(slot.arrival,arrival);
    if(!sdu.status) {std::memcpy(slot.payload[lane].data(),sdu.bytes.data(),sdu.size);slot.valid|=bit;}
    return true;
  }
  void concealed(uint16_t sequence) {
    if(!published || ahead(sequence,previous)) {published=true;previous=sequence;}
    for(auto &slot:slots) if(slot.present && !ahead(slot.sequence,previous)) {slot.present=slot.valid=0;counters.late++;}
  }
  bool take(uint64_t now,bool expected_valid,uint16_t expected,uint64_t due,
            IsoSdu &out,uint8_t &valid,uint64_t &arrival) {
    Slot *selected=nullptr;
    for(auto &slot:slots) {
      if(!slot.present) continue;
      if(now>=slot.arrival && now-slot.arrival>maximum_age) {slot.present=slot.valid=0;counters.expired++;restarting=true;continue;}
      if(published && !ahead(slot.sequence,previous)) {slot.present=slot.valid=0;counters.late++;continue;}
      if(!selected || ahead(selected->sequence,slot.sequence)) selected=&slot;
    }
    if(!selected) return false;
    auto deadline=selected->arrival+grace;
    if(expected_valid && selected->sequence==expected && due) deadline=std::min(deadline,due);
    if(selected->present!=3 && now<deadline) return false;
    out={};out.generation=epoch;out.sequence=selected->sequence;
    out.size=uint16_t(octets*blocks*2);out.discontinuity=restarting;
    valid=selected->valid;arrival=selected->arrival;
    for(uint32_t block=0;block<blocks;++block) for(unsigned lane=0;lane<2;++lane) {
      auto *destination=out.bytes.data()+(block*2+lane)*octets;
      if(valid&(1u<<lane)) std::memcpy(destination,selected->payload[lane].data()+block*octets,octets);
      else std::memset(destination,0,octets);
    }
    published=true;previous=selected->sequence;restarting=false;
    if(selected->present!=3 || valid!=3) counters.partial++;
    selected->present=selected->valid=0;counters.completed++;return true;
  }
  uint64_t deadline(bool expected_valid,uint16_t expected,uint64_t due) const {
    uint64_t result=0;
    for(const auto &slot:slots) if(slot.present) {
      auto next=slot.arrival+grace;
      if(expected_valid && slot.sequence==expected && due) next=std::min(next,due);
      if(!result || next<result) result=next;
    }
    return result;
  }
};
} // namespace bh
