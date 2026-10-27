/* Included inside AudioGraph. Exclusive offline replay of the live native LC3
 * source, controller-credit port, receive worker and PCM endpoint bridge. */
void start_iso_replay(const json &options) {
  require(offline && !replaying && iso_transmitter->prepared(),"Exclusive prepared offline LE source required");
  if(replay_worker.joinable()) replay_worker.join();
  uint16_t handle;uint32_t generation;
  require(iso_transmit_route(handle,generation),"Bind the offline LE source first");
  auto settings=iso_transmitter->snapshot();
  uint32_t rate=settings.at("rate"),duration=settings.at("duration_us"),octets=settings.at("octets"),blocks=settings.at("blocks");
  auto seconds=options.value("seconds",2.0);
  auto stall_ms=options.value("transport_stall_ms",0u);
  require(seconds>=.25 && seconds<=60 && stall_ms<=2000,"Invalid native LE replay interval");
  require(iso_handle(handle),"Prepare matching offline LE receiver for duplex replay");
  replay_stop=false;replaying=true;replay_started=ticks();
  {std::lock_guard<std::mutex> lock(replay_mutex);replay_result={{"done",false}};}
  replay_worker=std::thread([this,seconds,stall_ms,rate,duration,octets,blocks,handle,generation] {
    json result={{"done",true},{"scope","Native offline LC3 duplex/credits/capture; no USB or physical audio"}};
    try {
      Priority priority;Timer timer;
      auto create=api.voice.get<decltype(&bh_lc3_create_ex)>("bh_lc3_create_ex");
      auto destroy=api.voice.get<decltype(&bh_lc3_destroy)>("bh_lc3_destroy");
      auto encode=api.voice.get<decltype(&bh_lc3_encode)>("bh_lc3_encode");
      auto decode=api.voice.get<decltype(&bh_lc3_decode)>("bh_lc3_decode");
      std::unique_ptr<bh_lc3_stream,decltype(destroy)> codec(create(rate,duration,1,octets,BH_LC3_ENCODE|BH_LC3_DECODE),destroy);
      require(codec!=nullptr,"Offline LC3 reference unavailable");
      IsoSender sender;require(sender.configure(67,3),"Offline controller credits unavailable");
      IsoSduAssembler assembled;require(assembled.bind(handle,generation,uint16_t(octets*blocks)),"Offline SDU receiver unavailable");
      std::array<uint8_t,4104> packet{};
      std::array<float,960> pcm{},rendered{};
      std::array<uint8_t,800> encoded{};
      uint64_t origin=ticks(),quantum=0,capture_at=0,receive_at=0,rx_sequence=0,tx_sdus=0,tx_samples=0,rx_samples=0,wire_packets=0;
      uint64_t source_position=0,decode_errors=0;double energy=0,peak=0;
      bool held=false;uint64_t completion_due=0;
      while(!replay_stop && quantum*.001<seconds) {
        timer.until(origin+frequency()*quantum/1000);
        auto elapsed=age_ticks(origin);
        if(capture_at<=quantum) {
          endpoints->offline_capture(480,capture_at*48);
          capture_at=quantum+10;
        }
        if(receive_at<=quantum) {
          auto samples=rate*duration/1000000;
          for(uint32_t b=0;b<blocks;++b) {
            for(uint32_t i=0;i<samples;++i) pcm[i]=.1f*std::sin((source_position+i)*6.283185307179586*700/rate);
            require(encode(codec.get(),pcm.data(),samples,encoded.data()+b*octets,octets)==0,"Offline reference encoding failed");
            source_position+=samples;
          }
          uint32_t size=octets*blocks;
          put16(packet.data(),uint16_t(handle|0x2000));put16(packet.data()+2,uint16_t(size+4));
          put16(packet.data()+4,uint16_t(rx_sequence++));put16(packet.data()+6,uint16_t(size));
          std::memcpy(packet.data()+8,encoded.data(),size);
          require(iso_packet(packet.data(),size+8),"Offline native receive route retired");
          receive_at=quantum+duration*blocks/1000;
        }
        // Holding the simulated controller exercises the exact live credit
        // ledger without a Python packet producer/consumer or timeout refunds.
        bool blocked=stall_ms && elapsed>=frequency()/2 && elapsed<frequency()/2+frequency()*stall_ms/1000;
        if(!blocked) {
          if(held && elapsed>=completion_due) {
            uint16_t debt=sender.outstanding();
            if(debt) {uint8_t complete[]{0x13,5,1,uint8_t(handle),uint8_t(handle>>8),uint8_t(debt),uint8_t(debt>>8)};
              require(sender.event(complete,sizeof complete),"Offline completion rejected");}
            held=false;
          }
          uint32_t submitted=0;
          for(unsigned i=0;i<8;++i) {
            auto n=sender.output(*this,packet.data(),packet.size());
            if(!n) break;
            ++submitted;++wire_packets;
            require(assembled.feed(packet.data(),n,generation,[&](const IsoSdu &sdu) {
              ++tx_sdus;
              require(sdu.size==octets*blocks,"Native source emitted invalid LC3 SDU length");
              for(uint32_t b=0;b<blocks;++b) {
                auto count=rate*duration/1000000;
                if(decode(codec.get(),sdu.bytes.data()+b*octets,octets,pcm.data(),count)<0) {++decode_errors;continue;}
                for(uint32_t i=0;i<count;++i) {require(std::isfinite(pcm[i]),"Nonfinite transmitted PCM");energy+=pcm[i]*pcm[i];peak=std::max(peak,std::abs(double(pcm[i])));}
                tx_samples+=count;
              }
            }),"Native source emitted malformed ISO packet");
          }
          if(submitted && !held) {held=true;completion_due=elapsed+frequency()/200;}
        }
        if(quantum%10==0) {api.bh_mix_render(mixer,rendered.data(),480,api.bh_mix_master_value(mixer));rx_samples+=480;}
        ++quantum;
      }
      if(auto debt=sender.outstanding()) {
        uint8_t complete[]{0x13,5,1,uint8_t(handle),uint8_t(handle>>8),uint8_t(debt),uint8_t(debt>>8)};
        require(sender.event(complete,sizeof complete),"Final native ISO credit accounting failed");
      }
      result.update({{"tx_sdus",tx_sdus},{"tx_samples",tx_samples},{"tx_packets",wire_packets},{"rx_samples",rx_samples},
                     {"decode_errors",decode_errors},{"invalid",sender.invalid},{"rms",std::sqrt(energy/std::max<uint64_t>(1,tx_samples))},{"peak",peak},
                     {"completed",sender.completed()},{"outstanding",sender.outstanding()},{"passed",true}});
    } catch(const std::exception &error) {result["passed"]=false;result["error"]=error.what();}
    {std::lock_guard<std::mutex> lock(replay_mutex);replay_result=std::move(result);}
    replaying=false;
  });
}
