/* Native controller/QPC frequency fit. SPDX-License-Identifier: Apache-2.0.
 * Control feedback only: no PCM resampling, phase jumps or added buffering.
 * Sparse/batched USB events carry uncertain age. Median pairwise slopes reject
 * that jitter; only the codec worker updates the bounded, smoothly slewed rate.
 * Bluetooth Core ISOAL 3.1: TX_Time_Stamp + Time_Offset is a controller anchor.
 */
#pragma once
#include <array>
#include <algorithm>
#include <cmath>
#include <cstdint>
namespace bh {
class IsoClock {
  struct Point {double controller=0,host=0;};
  std::array<Point,64> points{};
  uint32_t size=0,cursor=0,previous_stamp=0;
  uint16_t previous_sequence=0;
  uint64_t origin_host=0,last_host=0,controller_time=0;
  double qpc_frequency=0,filtered=1.0;
public:
  uint64_t accepted=0,rejected=0,discontinuities=0;
  explicit IsoClock(double frequency):qpc_frequency(frequency) {}
  void clear() {
    size=cursor=previous_stamp=previous_sequence=0;origin_host=last_host=controller_time=0;
    filtered=1.0;accepted=rejected=discontinuities=0;
  }
  double scale() const {return filtered;}
  bool locked() const {return size>=4 && accepted>=4;}
  uint64_t last() const {return last_host;}
  bool observe(uint16_t sequence,uint32_t timestamp,uint32_t offset,uint64_t host) {
    if(offset>0xffffff || !host || !(qpc_frequency>0)) {rejected++;return false;}
    auto stamp=uint32_t(timestamp+offset);
    if(size) {
      auto sequences=uint16_t(sequence-previous_sequence);
      auto delta=uint32_t(stamp-previous_stamp);
      if(!sequences || sequences>0x7fff || !delta || delta>0x7fffffff || host<=last_host) {rejected++;return false;}
      if(delta>30000000 || double(host-last_host)/qpc_frequency>30) {
        // Keep frequency holdover; a long feedback absence never resets audio.
        size=cursor=0;controller_time=0;discontinuities++;
      } else controller_time+=delta;
    }
    if(!size) {origin_host=host;controller_time=0;}
    previous_stamp=stamp;previous_sequence=sequence;last_host=host;
    points[cursor]={double(controller_time),double(host-origin_host)*1000000/qpc_frequency};
    cursor=(cursor+1)%points.size();size=std::min<uint32_t>(size+1,points.size());accepted++;
    std::array<double,2016> slopes{};uint32_t count=0;
    for(uint32_t a=0;a<size;++a) for(uint32_t b=a+1;b<size;++b) {
      auto dx=points[b].controller-points[a].controller;
      if(std::abs(dx)<2000000) continue;
      auto slope=(points[b].host-points[a].host)/dx;
      if(std::isfinite(slope)) slopes[count++]=slope;
    }
    if(count) {
      auto middle=slopes.begin()+count/2;
      std::nth_element(slopes.begin(),middle,slopes.begin()+count);
      auto measured=*middle;
      if(measured>=.995 && measured<=1.005) {
        measured=std::clamp(measured,.998,1.002);
        filtered+=std::clamp(measured-filtered,-.00015,.00015);
      } else rejected++;
    }
    return true;
  }
};
} // namespace bh
