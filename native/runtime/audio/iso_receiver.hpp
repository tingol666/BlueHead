/* Native LC3 playout. SPDX-License-Identifier: Apache-2.0.
 * USB only assembles/copies SDUs. This worker owns the single receive timeline,
 * optional split-channel alignment, bounded per-channel PLC and float mixer. */
#pragma once
#include "media_worker.hpp"
#include "iso_sdu.hpp"
#include "iso_playout.hpp"
#include "iso_channels.hpp"
#include "lc3_stream.h"
namespace bh {
struct Lc3Api {
  decltype(&bh_lc3_abi) abi;
  decltype(&bh_lc3_create_ex) create;
  decltype(&bh_lc3_destroy) destroy;
  decltype(&bh_lc3_reset_rx) reset;
  decltype(&bh_lc3_decode) decode;
  decltype(&bh_lc3_decode_mask) decode_mask;
  decltype(&bh_lc3_reset_rx_channel) reset_channel;
  explicit Lc3Api(Module &module)
      : abi(module.get<decltype(abi)>("bh_lc3_abi")),create(module.get<decltype(create)>("bh_lc3_create_ex")),
        destroy(module.get<decltype(destroy)>("bh_lc3_destroy")),reset(module.get<decltype(reset)>("bh_lc3_reset_rx")),
        decode(module.get<decltype(decode)>("bh_lc3_decode")),decode_mask(module.get<decltype(decode_mask)>("bh_lc3_decode_mask")),
        reset_channel(module.get<decltype(reset_channel)>("bh_lc3_reset_rx_channel")) {
    require(abi()==1,"Unsupported LC3 stream ABI");
  }
};
class IsoReceiver final {
  struct Frame {IsoSdu sdu;uint64_t arrival=0;uint8_t lane=0,valid=0xff;};
  MediaServices &core;
  int input;
  std::unique_ptr<Lc3Api> api;
  bh_lc3_stream *decoder=nullptr;
  std::array<IsoSduAssembler,2> assemblers;
  Spsc<Frame,32> queue;
  std::thread worker;
  std::unique_ptr<Timer> timer;
  IsoPlayout timeline;
  IsoChannels group;
  Handle wake{CreateEventW(nullptr,FALSE,FALSE,nullptr)};
  std::atomic<bool> stop{true},failed{false};
  std::atomic<uint32_t> handle{0xffff},right_handle{0xffff},parent{0xffff},epoch{0},producers{0},stall{0};
  uint32_t rate=0,duration=0,channels=0,octets=0,blocks=0,samples=0,allocation=0;
  bool voice=false,split=false,broadcast=false;
  std::atomic<uint64_t> packets{0},drops{0},errors{0},frames{0},concealed{0},resets{0},expired{0};
  std::atomic<uint64_t> coded_bytes{0},last_packet{0},bitrate{0},group_expired{0},group_late{0};
  uint64_t meter_start=0,meter_bytes=0,meter_packets=0;
  std::atomic<double> packets_per_second{0};
  std::atomic<uint64_t> malformed{0},late{0},duplicate{0},partial{0},unsynchronized{0},group_resets{0},silent_channels{0},channel_resets{0};
  bool active() const {return !stop && (handle!=0xffff || right_handle!=0xffff);}
  void write(const float *pcm) {
    if(channels==1 && (allocation==1 || allocation==2)) {
      std::array<float,960> stereo{};
      for(uint32_t i=0;i<samples;++i) stereo[2*i+(allocation==2?1:0)]=pcm[i];
      core.api.bh_mix_write(core.mixer,input,stereo.data(),samples);
    } else if(channels==1) core.api.bh_mix_write_f32_mono(core.mixer,input,pcm,samples);
    else core.api.bh_mix_write(core.mixer,input,pcm,samples);
  }
  void run() {
    Priority priority;
    Frame frame,joined;
    std::array<float,960> pcm{};
    std::array<uint32_t,2> missing{};
    std::array<bool,2> silent{};
    uint64_t position=0;
    const uint64_t maximum_age=frequency()*(voice?90:180)/1000;
    auto reset=[&] {
      if(api->reset(decoder)<0) {errors++;timeline.invalidate();return false;}
      core.api.bh_mix_clear(core.mixer,input);resets++;position=0;missing.fill(0);silent.fill(false);return true;
    };
    auto decode=[&](const uint8_t *data,uint32_t bytes,uint64_t arrival,uint8_t valid) {
      int result;
      if(split) {
        auto all=uint32_t((1u<<channels)-1u);
        uint32_t conceal=all&~valid;
        for(uint32_t channel=0;channel<channels;++channel) {
          auto bit=1u<<channel;
          if(valid&bit) {
            if(silent[channel]) {
              if(api->reset_channel(decoder,channel)<0) {errors++;timeline.invalidate();return false;}
              silent[channel]=false;channel_resets++;
            }
            missing[channel]=0;
          } else if(++missing[channel]>40000/duration) {
            conceal&=~bit;silent[channel]=true;silent_channels++;
          }
        }
        result=api->decode_mask(decoder,valid?data:nullptr,valid?bytes:0,valid,conceal,pcm.data(),samples);
      } else result=api->decode(decoder,data,bytes,pcm.data(),samples);
      if(result<0) {errors++;timeline.invalidate();return false;}
      if(result) concealed++;
      if(!active()) return false;
      if(arrival && age_ticks(arrival)>maximum_age) {expired++;timeline.invalidate();return false;}
      write(pcm.data());frames+=samples;position+=samples;return true;
    };
    auto process=[&](const Frame &item) {
      if(!active() || item.sdu.generation!=epoch) return;
      if(split && item.sdu.discontinuity) timeline.reanchor();
      auto decision=timeline.observe(item.sdu.sequence,item.arrival,ticks());
      if(decision.expired) expired++;
      if(!decision.accept || (decision.reset && !reset())) return;
      bool valid=true;
      for(uint32_t i=0;i<decision.gap*blocks && !stop;++i)
        if(!decode(nullptr,0,item.arrival,0)) {valid=false;break;}
      if(!valid || stop) return;
      uint32_t frame_bytes=octets*channels;
      bool erased=item.sdu.status!=0 || item.sdu.size!=frame_bytes*blocks;
      if(!item.sdu.status && erased) errors++;
      uint8_t mask=erased?0:uint8_t(item.valid&((1u<<channels)-1u));
      for(uint32_t i=0;i<blocks && !stop;++i)
        if(!decode(mask?item.sdu.bytes.data()+i*frame_bytes:nullptr,mask?frame_bytes:0,item.arrival,mask)) break;
      if(active()) core.api.bh_mix_observe(core.mixer,input,bht_scale(position,rate,BHT_RATE),item.arrival);
    };
    while(!stop) {
      auto pause=stall.exchange(0);
      if(pause) {core.injected_stalls++;auto until=ticks()+frequency()*pause/1000;while(!stop && ticks()<until)Sleep(1);}
      bool received=queue.pop(frame);
      if(received && active() && frame.sdu.generation==epoch) {
        if(split) group.feed(frame.lane,frame.sdu,frame.arrival);
        else process(frame);
      }
      if(split) {
        for(unsigned batch=0;batch<8 && !stop;++batch) {
          if(!group.take(ticks(),timeline.sequenced_value(),timeline.next_sequence(),timeline.deadline(),
                         joined.sdu,joined.valid,joined.arrival)) break;
          process(joined);
        }
        partial=group.counters.partial;unsynchronized=group.counters.unsynchronized;group_resets=group.counters.resets;
        group_expired=group.counters.expired;group_late=group.counters.late;
      }
      if(received) continue;
      auto expected=timeline.next_sequence();
      if(active() && timeline.missing(ticks())) {
        if(split) group.concealed(expected);
        for(uint32_t i=0;i<blocks && !stop;++i) if(!decode(nullptr,0,0,0)) break;
        continue;
      }
      auto due=timeline.deadline();
      if(split) {
        auto channel_due=group.deadline(timeline.sequenced_value(),timeline.next_sequence(),due);
        if(channel_due && (!due || channel_due<due)) due=channel_due;
      }
      if(due) timer->wait(due,wake);
      else WaitForSingleObject(wake,INFINITE);
    }
  }
public:
  explicit IsoReceiver(MediaServices &services,int slot):core(services),input(slot) {require(bool(wake),"Cannot create ISO receiver wake event");}
  ~IsoReceiver() {retire();}
  void retire() {
    handle=right_handle=parent=0xffff;
    core.api.bh_mix_connected(core.mixer,input,false);
    while(producers.load()) SwitchToThread();
    stop=true;SetEvent(wake);if(worker.joinable())worker.join();
    for(auto &assembler:assemblers) assembler.unbind();
    Frame discard;while(queue.pop(discard)){}
    if(decoder)api->destroy(decoder);
    decoder=nullptr;api.reset();timer.reset();epoch=0;
  }
  void configure(uint32_t hz,uint32_t dt,uint32_t ch,uint32_t bytes,uint32_t count,
                 uint32_t generation,bool call,uint32_t wait_us=3000,bool split_channels=false,
                 bool is_broadcast=false,uint32_t locations=0,bool timestamps=true,uint16_t pending_parent=0xffff) {
    require(!active(),"Stop LE stream before reconfiguration");
    require(generation && count>=1 && count<=2 && wait_us>=1000 && wait_us<=20000,
            "Invalid LC3 generation/blocks/wait");
    require((!split_channels || ch==2) && !(locations&~3u) && (ch!=1 || locations!=3),"Invalid LC3 channel allocation");
    auto functions=std::make_unique<Lc3Api>(core.api.voice);
    std::unique_ptr<bh_lc3_stream,decltype(functions->destroy)> prepared(functions->create(hz,dt,ch,bytes,BH_LC3_DECODE),functions->destroy);
    require(prepared!=nullptr,"Unsupported LC3 receive configuration");
    auto prepared_timer=std::make_unique<Timer>();
    retire();
    require(core.api.bh_mix_format(core.mixer,input,hz)==0,"Cannot configure LC3 output rate");
    rate=hz;duration=dt;channels=ch;octets=bytes;blocks=count;samples=rate*duration/1000000;
    voice=call;split=split_channels;broadcast=is_broadcast;allocation=locations;epoch=generation;
    api=std::move(functions);decoder=prepared.release();
    packets=drops=errors=frames=concealed=resets=expired=malformed=late=duplicate=0;
    partial=unsynchronized=group_resets=silent_channels=channel_resets=0;
    coded_bytes=last_packet=bitrate=group_expired=group_late=0;meter_start=meter_bytes=meter_packets=0;packets_per_second=0;failed=false;timer=std::move(prepared_timer);
    require(timeline.configure(frequency()*duration*blocks/1000000,frequency()*wait_us/1000000,
                              frequency()*(voice?90:180)/1000,std::min(8u,std::max(1u,40000u/(duration*blocks)))),"Invalid ISO playout clock");
    if(split) require(group.configure(generation,octets,blocks,duration*blocks,frequency()*wait_us/1000000,
                                    frequency()*(voice?90:180)/1000,timestamps?IsoChannels::Clock::ControllerTimestamp:IsoChannels::Clock::SharedSequence),
                       "Invalid split-channel clock");
    core.api.bh_mix_clear(core.mixer,input);parent=pending_parent;stop=false;
    // Reopen only for an explicit new decoder generation, never from a late
    // bind or UI gate after native disconnect has already closed the route.
    core.api.bh_mix_connected(core.mixer,input,true);
    try {worker=std::thread([this]{try{run();}catch(...){failed=true;errors++;transport_lost();}});}
    catch(...) {retire();throw;}
  }
  bool is_broadcast() const {return broadcast;}
  void bind(uint16_t h,uint16_t acl,uint32_t generation,uint32_t lane=0) {
    require(decoder && !stop && epoch==generation && lane<(split?2u:1u),"Retired or unprepared LC3 stream");
    auto &selected=lane?right_handle:handle;
    require(selected==0xffff && (acl<=0xeff || (broadcast && acl==0xffff)) && (parent==0xffff || parent==acl),"Invalid ISO parent/channel binding");
    require(assemblers[lane].bind(h,generation,uint16_t(octets*blocks*(split?1:channels))),"Invalid ISO stream binding");
    parent=acl;selected=h;
  }
  void transport_lost() noexcept {handle=right_handle=parent=0xffff;stop=true;SetEvent(wake);core.api.bh_mix_connected(core.mixer,input,false);}
  void disconnect(uint16_t h) noexcept {if(handle==h || right_handle==h || parent==h)transport_lost();}
  bool prepared() const {return epoch!=0;}
  bool generation(uint32_t expected) const {return epoch==expected && expected;}
  bool owns(uint16_t h) const {return h!=0xffff && (handle==h || right_handle==h) && !stop;}
  bool current(uint32_t generation) const {return epoch==generation && !stop;}
  bool push(const uint8_t *p,uint32_t n) {
    CounterGuard guard(producers);bool accepted=false;
    if(p && n>=4 && owns(le16(p)&0xfff)) {
      accepted=true;auto lane=right_handle==(le16(p)&0xfff)?1u:0u;auto &assembler=assemblers[lane];
      auto before=assembler.counters;
      assembler.feed(p,n,epoch,[&](const IsoSdu &sdu) {
        Frame frame;frame.sdu=sdu;frame.arrival=ticks();frame.lane=uint8_t(lane);packets++;
        last_packet=frame.arrival;coded_bytes+=sdu.size;
        if(!meter_start) {meter_start=frame.arrival;meter_bytes=coded_bytes;meter_packets=packets;}
        if(frame.arrival-meter_start>=frequency()/3) {
          bitrate=(coded_bytes.load()-meter_bytes)*8*frequency()/(frame.arrival-meter_start);
          auto count=packets.load();
          packets_per_second=(count-meter_packets)*static_cast<double>(frequency())/(frame.arrival-meter_start);
          meter_start=frame.arrival;meter_bytes=coded_bytes;meter_packets=count;
        }
        if(!queue.push(frame))drops++;else SetEvent(wake);
      });
      malformed+=assembler.counters.malformed-before.malformed;
      duplicate+=assembler.counters.duplicate-before.duplicate;
      late+=assembler.counters.late-before.late;
    }
    return accepted;
  }
  void inject_stall(uint32_t ms) {stall=ms;SetEvent(wake);}
  json snapshot() const {
    return {{"active",active()},{"epoch",epoch.load()},{"handle",handle.load()},{"right_handle",right_handle.load()},
            {"parent",parent.load()},{"rate",rate},{"duration_us",duration},{"channels",channels},{"octets",octets},{"blocks",blocks},
            {"split",split},{"broadcast",broadcast},{"allocation",allocation},{"packets",packets.load()},{"dropped",drops.load()},
            {"errors",errors.load()},{"malformed",malformed.load()},{"duplicate",duplicate.load()},{"late",late.load()},{"failed",failed.load()},
            {"frames",frames.load()},{"concealed_blocks",concealed.load()},{"resets",resets.load()},{"expired",expired.load()},
            {"partial_channels",partial.load()},{"unsynchronized",unsynchronized.load()},{"group_resets",group_resets.load()},
            {"silent_channels",silent_channels.load()},{"channel_resets",channel_resets.load()},
            {"group_expired",group_expired.load()},{"group_late",group_late.load()},
            {"coded_bytes",coded_bytes.load()},{"bitrate",active() && last_packet && age_ticks(last_packet.load())<frequency()/2?bitrate.load():0},
            {"packets_per_second",active() && last_packet && age_ticks(last_packet.load())<frequency()/2?packets_per_second.load():0},
            {"buffer_ms",core.api.bh_mix_value(core.mixer,input,0)/48.0},
            {"last_packet_age_ms",last_packet?age_ticks(last_packet.load())*1000/frequency():uint64_t(1000000000)}};
  }
};
} // namespace bh
