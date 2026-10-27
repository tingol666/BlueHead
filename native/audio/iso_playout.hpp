/* SDU sequence and deadline policy. SPDX-License-Identifier: Apache-2.0.
 * Clock units are supplied by the caller; no I/O, codec, PLC or allocation.
 * Missing SDUs are requested once. Late originals cannot replace concealed PCM. */
#pragma once
#include <cstdint>
namespace bh {
class IsoPlayout {
  uint64_t interval = 0, grace = 0, maximum_age = 0, due = 0;
  uint16_t previous = 0;
  uint32_t concealed = 0, limit = 0;
  bool sequenced = false, recovering = false;
public:
  struct Decision { bool accept = false, reset = false, expired = false; uint32_t gap = 0; };
  bool configure(uint64_t period, uint64_t wait, uint64_t age, uint32_t maximum_conceal) {
    if (!period || wait > age || period > age || !maximum_conceal || maximum_conceal > 8) return false;
    interval = period; grace = wait; maximum_age = age; limit = maximum_conceal;
    due = 0; previous = 0; concealed = 0; sequenced = recovering = false;
    return true;
  }
  void invalidate() { recovering = true; due = 0; }
  uint64_t deadline() const { return due; }
  bool sequenced_value() const {return sequenced;}
  uint16_t next_sequence() const {return uint16_t(previous+1);}
  void reanchor() {recovering=true;sequenced=false;due=0;concealed=0;}

  Decision observe(uint16_t sequence, uint64_t arrival, uint64_t now) {
    if (!interval) return {};
    if (now >= arrival && now - arrival > maximum_age) {
      invalidate(); return {false, false, true, 0};
    }
    uint16_t distance = sequenced ? uint16_t(sequence - previous) : 1;
    if (!distance || distance >= 0x8000) return {};
    bool reset = recovering || distance - 1u > limit;
    previous = sequence; sequenced = true; recovering = false; concealed = 0;
    due = arrival + interval + grace;
    return {true, reset, false, reset ? 0u : distance - 1u};
  }
  bool missing(uint64_t now) {
    if (!due || now < due) return false;
    if (concealed >= limit || now - due > maximum_age) { invalidate(); return false; }
    previous++; concealed++; due += interval;
    return true;
  }
};
} // namespace bh
