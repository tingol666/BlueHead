/* Native LC3 microphone/PC-mix source. SPDX-License-Identifier: Apache-2.0.
 * Reuses the sole EndpointActor/uplink clock and capture rings. Never opens
 * another input, USB handle or HCI command queue. Only the USB owner calls
 * output(); control/codec work never runs in a USB callback. */
#pragma once
#include "runtime_api.hpp"
#include "lc3_stream.h"
#include "iso_tx.hpp"
#include "iso_clock.hpp"
namespace bh {
class IsoTransmitter {
  struct Functions {
    decltype(&bh_lc3_abi) abi;
    decltype(&bh_lc3_create_ex) create;
    decltype(&bh_lc3_destroy) destroy;
    decltype(&bh_lc3_encode) encode;
    decltype(&bh_lc3_reset_tx) reset;
    explicit Functions(Module &m)
      : abi(m.get<decltype(abi)>("bh_lc3_abi")), create(m.get<decltype(create)>("bh_lc3_create_ex")),
        destroy(m.get<decltype(destroy)>("bh_lc3_destroy")), encode(m.get<decltype(encode)>("bh_lc3_encode")),
        reset(m.get<decltype(reset)>("bh_lc3_reset_tx")) { require(abi()==1,"Unsupported LC3 source ABI"); }
  };
  struct Frame {
    std::array<uint8_t,800> bytes{};
    uint32_t epoch=0, size=0;
    uint16_t sequence=0;
    uint64_t deadline=0;
  };
  Api &api;
  void *uplink;
  const std::atomic<float> &mic_gain, &pc_gain, &peer_gain;
  const std::atomic<bool> &muted;
  std::unique_ptr<Functions> functions;
  bh_lc3_stream *encoder=nullptr;
  uint32_t rate=0, duration=0, octets=0, blocks=0, samples=0;
  std::atomic<uint32_t> handle{0xffff}, parent{0xffff}, epoch{0}, consumers{0}, stall{0};
  std::atomic<bool> stop{true}, failed{false};
  Handle wake{CreateEventW(nullptr,FALSE,FALSE,nullptr)};
  Spsc<Frame,8> queue;
  struct ClockSample {uint32_t epoch=0,timestamp=0,offset=0;uint16_t sequence=0;uint64_t host=0;};
  Spsc<ClockSample,16> clocks;
  std::atomic<double> clock_ppm{0};
  std::atomic<uint64_t> clock_samples{0},clock_rejected{0},clock_last{0};
  std::atomic<bool> clock_locked{false};
  std::thread worker;
  std::unique_ptr<Timer> timer;
  IsoSduFragmenter fragmenter; // Solely USB-consumer-owned, including on rebind.
  uint32_t wire_epoch=0;
  uint16_t wire_handle=0xffff, wire_limit=0;
  uint64_t wire_deadline=0;
  std::atomic<uint64_t> frames{0}, encoded{0}, errors{0}, dropped{0}, expired{0}, resets{0}, sent{0};
  std::atomic<uint64_t> wire_bytes{0},bitrate{0},last_packet{0};
  uint64_t meter_start=0,meter_bytes=0;
  void run() {
    Priority priority;
    std::array<float,960> pcm{};
    Frame frame;
    uint64_t next=0;
    uint16_t sequence=0;
    const uint64_t nominal=frequency()*duration*blocks/1000000;
    uint64_t interval=nominal;
    IsoClock clock{double(frequency())};
    while(!stop) {
      auto pause=stall.exchange(0);
      if(pause) {auto until=ticks()+frequency()*pause/1000;while(!stop && ticks()<until) Sleep(1);}
      if(handle==0xffff) {next=0;WaitForSingleObject(wake,INFINITE);continue;}
      ClockSample observation;
      for(unsigned i=0;i<16 && clocks.pop(observation);++i)
        if(observation.epoch==epoch)
          clock.observe(observation.sequence,observation.timestamp,observation.offset,observation.host);
      interval=std::max<uint64_t>(1,uint64_t(std::llround(nominal*clock.scale())));
      clock_ppm=(clock.scale()-1)*1000000;clock_samples=clock.accepted;clock_rejected=clock.rejected;
      clock_last=clock.last();clock_locked=clock.locked();
      auto now=ticks();
      if(!next) next=now;
      if(now<next) {timer->wait(next,wake);continue;}
      if(now-next>=2*interval) {
        auto skipped=(now-next)/interval;
        sequence=uint16_t(sequence+skipped);next+=skipped*interval;expired+=skipped;
        require(functions->reset(encoder)==0,"LC3 source reset failed");resets++;
      }
      api.bh_up_gain(uplink,mic_gain.load(),pc_gain.load(),muted?0.0f:peer_gain.load(),muted.load());
      auto count=samples*blocks;
      if(api.bh_up_read_f32(uplink,pcm.data(),count)!=int(count)) {errors++;next+=interval;continue;}
      frame.epoch=epoch;frame.sequence=sequence++;frame.size=octets*blocks;
      frame.deadline=next+2*interval;
      bool valid=true;
      for(uint32_t i=0;i<blocks;++i)
        if(functions->encode(encoder,pcm.data()+i*samples,samples,frame.bytes.data()+i*octets,octets)<0) {valid=false;break;}
      if(valid && !stop && handle!=0xffff) {
        if(!queue.push(frame)) dropped++;
        else {frames+=count;encoded+=frame.size;}
      } else if(!valid) errors++;
      next+=interval;
    }
  }
public:
  IsoTransmitter(Api &a,void *up,const std::atomic<float> &mic,const std::atomic<float> &pc,
                 const std::atomic<float> &peer,const std::atomic<bool> &mute)
    :api(a),uplink(up),mic_gain(mic),pc_gain(pc),peer_gain(peer),muted(mute) {require(bool(wake),"LC3 source event unavailable");}
  ~IsoTransmitter() {retire();}
  void prepare(uint32_t hz,uint32_t dt,uint32_t bytes,uint32_t count,uint32_t generation,uint16_t pending_parent=0xffff) {
    require(generation && count>=1 && count<=2 && !prepared(),"LC3 source still owns the microphone");
    auto f=std::make_unique<Functions>(api.voice);
    std::unique_ptr<bh_lc3_stream,decltype(f->destroy)> candidate(f->create(hz,dt,1,bytes,BH_LC3_ENCODE),f->destroy);
    require(candidate!=nullptr,"Unsupported LC3 microphone configuration");
    auto prepared_timer=std::make_unique<Timer>();
    rate=hz;duration=dt;octets=bytes;blocks=count;samples=hz*dt/1000000;
    functions=std::move(f);encoder=candidate.release();timer=std::move(prepared_timer);epoch=generation;
    frames=encoded=errors=dropped=expired=resets=sent=0;
    clock_ppm=0;clock_samples=clock_rejected=clock_last=0;clock_locked=false;
    wire_bytes=bitrate=last_packet=0;meter_start=meter_bytes=0;
    ClockSample discarded;while(clocks.pop(discarded)){}
    parent=pending_parent;failed=false;stop=false;
    try {worker=std::thread([this]{try{run();}catch(...){errors++;failed=true;transport_lost();}});}
    catch(...) {retire();throw;}
  }
  void bind(uint16_t h,uint16_t acl,uint32_t generation) {
    require(encoder && !stop && epoch==generation && handle==0xffff && (parent==0xffff || parent==acl) && h<=0xeff && acl<=0xeff && h!=acl,
            "Invalid or retired LC3 source binding");
    parent=acl;handle=h;SetEvent(wake);
  }
  void transport_lost() noexcept {handle=parent=0xffff;stop=true;SetEvent(wake);}
  void disconnect(uint16_t h) noexcept {if(handle==h || parent==h) transport_lost();}
  void retire() {
    handle=parent=0xffff;
    while(consumers.load()) SwitchToThread();
    stop=true;SetEvent(wake);if(worker.joinable())worker.join();
    Frame discard;while(queue.pop(discard)){}
    if(encoder) functions->destroy(encoder);
    encoder=nullptr;functions.reset();timer.reset();epoch=0;
  }
  bool prepared() const {return epoch!=0;}
  bool generation(uint32_t e) const {return e && e==epoch;}
  bool route(uint16_t &h,uint32_t &e) const {
    h=uint16_t(handle.load());e=epoch;return h!=0xffff && e && !stop;
  }
  // Called only by the sole USB owner (or its exclusive offline replay).
  uint32_t output(uint32_t payload_limit,uint8_t *out,uint32_t capacity,uint32_t &generation) {
    CounterGuard guard(consumers);uint16_t h;uint32_t e;
    if(!route(h,e) || payload_limit<5 || payload_limit>0x3fff || capacity<9) return 0;
    generation=e;
    if(wire_epoch!=e || wire_handle!=h || wire_limit!=payload_limit) {
      if(!fragmenter.bind(h,e,uint16_t(payload_limit))) return 0;
      wire_epoch=e;wire_handle=h;wire_limit=uint16_t(payload_limit);wire_deadline=0;
    }
    if(fragmenter.pending() && ticks()>wire_deadline) {fragmenter.cancel();expired++;}
    if(!fragmenter.pending()) {
      Frame frame;bool ready=false;
      for(unsigned i=0;i<8 && queue.pop(frame);++i) {
        if(frame.epoch!=e || ticks()>frame.deadline) {expired++;continue;}
        ready=fragmenter.start(e,frame.sequence,frame.bytes.data(),frame.size);wire_deadline=frame.deadline;break;
      }
      if(!ready) return 0;
    }
    auto n=fragmenter.next(e,out,capacity);
    if(handle!=h || epoch!=e || stop) return 0;
    if(n) {
      sent++;auto now=ticks();last_packet=now;
      auto boundary=(out[1]>>4)&3;
      uint32_t overhead=(boundary==0 || boundary==2)?4u+((out[1]&0x40)?4u:0u):0u;
      if(n>=4+overhead) wire_bytes+=n-4-overhead;
      if(!meter_start) {meter_start=now;meter_bytes=wire_bytes;}
      else if(now-meter_start>=frequency()/3) {
        bitrate=(wire_bytes.load()-meter_bytes)*8*frequency()/(now-meter_start);
        meter_start=now;meter_bytes=wire_bytes;
      }
      SetEvent(wake);
    }return n;
  }
  // Sole USB event owner is the only producer. Epoch rejects late completions.
  void controller_clock(uint16_t h,uint32_t generation,uint16_t sequence,uint32_t stamp,uint32_t offset,uint64_t host) {
    CounterGuard guard(consumers);
    if(handle==h && epoch==generation && !stop) {
      if(!clocks.push({generation,stamp,offset,sequence,host})) clock_rejected++;
      SetEvent(wake);
    }
  }
  void inject_stall(uint32_t ms) {stall=ms;SetEvent(wake);}
  json snapshot() const {
    return {{"active",handle!=0xffff && !stop},{"prepared",prepared()},{"epoch",epoch.load()},
            {"handle",handle.load()},{"parent",parent.load()},{"rate",rate},{"duration_us",duration},
            {"octets",octets},{"blocks",blocks},{"frames",frames.load()},{"coded_bytes",encoded.load()},
            {"packets",sent.load()},{"dropped",dropped.load()},{"expired",expired.load()},
            {"resets",resets.load()},{"errors",errors.load()},{"failed",failed.load()},{"peer_gain",peer_gain.load()},
            {"sent_coded_bytes",wire_bytes.load()},{"bitrate",last_packet && !stop && age_ticks(last_packet.load())<frequency()/2?bitrate.load():0},
            {"clock_ppm",clock_ppm.load()},{"clock_samples",clock_samples.load()},{"clock_rejected",clock_rejected.load()},
            {"clock_locked",clock_locked.load()},{"clock_age_ms",clock_last?age_ticks(clock_last.load())*1000/frequency():uint64_t(1000000000)}};
  }
};
} // namespace bh
