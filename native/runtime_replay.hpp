/* Included inside Core. Offline native transport/codec/mixer replay only. */
void start_replay(const json &options) {
  require(offline, "Fault injection is available only in offline-test mode");
  {
    std::lock_guard<std::mutex> lock(parameter_mutex);
    quiesced = false;
    api.bh_mix_master(mixer, playback_gain);
  }
  require(!replaying, "A native replay is already active");
  if (replay_worker.joinable())
    replay_worker.join();
  double duration = options.value("seconds", 5.0);
  require(duration >= .25 && duration <= 43200, "Replay duration out of bounds");
  double jitter = options.value("jitter_ms", 0.0) / 1000;
  require(jitter >= 0 && jitter <= .100, "Replay jitter out of bounds");
  double ppm = options.value("clock_ppm", 0.0);
  require(std::abs(ppm) <= 1000, "Replay clock mismatch out of bounds");
  unsigned drop_every = options.value("drop_every", 0u);
  unsigned selected_voice = options.value("voice_codec", 0u);
  require(selected_voice <= 3, "Invalid voice codec");
  std::string codec_name = options.value("codec", std::string("sbc"));
  std::ifstream vector_file(assets / L"selfcheck_vectors.json");
  auto fixture = json::parse(vector_file);
  json selected;
  for (const auto &v : fixture.at("vectors"))
    if (v.at("codec") == codec_name)
      selected = v;
  require(!selected.is_null() && codec_name != "ldac", "Choose a stable pinned replay codec");
  int code = codec_name == "sbc" ? 1 : codec_name == "aac" ? 2 : codec_name == "aptx" ? 3 : 4;
  std::vector<std::vector<uint8_t>> packets;
  for (const auto &encoded : selected.at("packets"))
    packets.push_back(base64(encoded.get<std::string>()));
  stop_voice();
  for (auto key : {"offline:media", "offline:damaged", "call:offline"})
    command({{"op", "mix_remove"}, {"key", key}}, assets);
  enhance_audio = !options.value("legacy_continuity", false);
  api.bh_up_continuity(uplink, enhance_audio);
  int id = command({{"op", "mix_add"}, {"key", "offline:media"}}, assets).at("index");
  api.bh_mix_continuity(mixer, id, enhance_audio, 1440, 5760);
  auto &slot = *media[id];
  slot.configure(code, 48000, 2, "offline:media");
  slot.handle = 1;
  slot.cid = 64;
  slot.paused = false;
  bool damaged_source = options.value("damaged_source", false);
  if (damaged_source) {
    require(code == 1, "Damaged competing-stream replay uses SBC");
    int bad = command({{"op", "mix_add"}, {"key", "offline:damaged"}}, assets).at("index");
    media[bad]->configure(1, 48000, 2, "offline:damaged");
    media[bad]->handle = 3;
    media[bad]->cid = 65;
    media[bad]->paused = false;
  }
  if (selected_voice) {
    command({{"op", "mix_add"}, {"key", "call:offline"}}, assets);
    command({{"op", "voice_start"}, {"key", "offline"}, {"handle", 2}, {"codec", selected_voice}},
            assets);
  }
  replay_stop = false;
  replaying = true;
  replay_started = ticks();
  {
    std::lock_guard<std::mutex> lock(replay_mutex);
    replay_result = {
        {"done", false},
        {"seconds", duration},
        {"scope",
         "Native synthetic HCI, codecs and PCM endpoints; no USB, playback or microphone opened"}};
  }
  replay_worker = std::thread([this, duration, jitter, ppm, drop_every,
                               packets = std::move(packets), id, code, selected_voice,
                               damaged_source] {
    std::atomic<uint64_t> received{0}, injected_loss{0}, rendered{0}, zero_frames{0},
        max_zero_run{0}, current_zero_run{0}, render_deadline_misses{0};
    std::atomic<uint64_t> minimum_queue{UINT64_MAX}, maximum_queue{0};
    uint64_t origin = ticks();
    std::thread producer([&, this] {
      Priority priority;
      Timer timer;
      HciDispatch dispatcher(*this);
      uint64_t sequence = 0;
      double packet_time = 0, previous_arrival = 0, next_voice = 0, next_mic = 0;
      uint32_t samples = code == 1 ? 128 : 1024;
      double packet_duration = samples / (48000 * (1 + ppm / 1000000));
      bh_voice *source_voice = selected_voice ? api.bh_voice_create(selected_voice) : nullptr;
      std::array<int16_t, 480> microphone{};
      std::array<int16_t, 360> downlink{};
      std::array<uint8_t, 120> wire{};
      std::array<uint8_t, 255> sent{};
      uint64_t voice_sequence = 0, mic_samples = 0;
      uint32_t random = 0x201a5e;
      while (!replay_stop) {
        double next =
            std::min(packet_time, selected_voice ? std::min(next_voice, next_mic) : packet_time);
        if (next >= duration)
          break;
        timer.until(origin + static_cast<uint64_t>(next * frequency()));
        if (selected_voice && next_mic <= next) {
          for (size_t i = 0; i < microphone.size(); i++)
            microphone[i] = static_cast<int16_t>(
                5000 * std::sin((mic_samples + i) * 6.283185307179586 * 700 / 48000));
          api.bh_up_write(uplink, 0, reinterpret_cast<const uint8_t *>(microphone.data()), 960);
          bridge_frames += 480;
          mic_samples += 480;
          next_mic += .010;
        }
        if (selected_voice && next_voice <= next) {
          for (size_t i = 0; i < downlink.size(); i++)
            downlink[i] = static_cast<int16_t>(
                5000 * std::sin((voice_sequence * 360 + i) * 6.283185307179586 * 1100 / 48000));
          int n = api.bh_voice_encode(source_voice, downlink.data(), 360, wire.data(), wire.size());
          if (n > 0)
            voice_packet(2, 0, wire.data(), n);
          uint16_t handle = 0;
          voice_output(handle, sent.data(), selected_voice == 1 ? 120 : 60);
          voice_sequence++;
          next_voice += .0075;
        }
        if (packet_time <= next) {
          auto data = packets[sequence % packets.size()];
          if (code != 3 && data.size() >= 12) {
            data[2] = static_cast<uint8_t>(sequence >> 8);
            data[3] = static_cast<uint8_t>(sequence);
            uint32_t stamp = static_cast<uint32_t>(sequence * samples);
            data[4] = stamp >> 24;
            data[5] = stamp >> 16;
            data[6] = stamp >> 8;
            data[7] = stamp;
          }
          if (drop_every && sequence && sequence % drop_every == 0)
            injected_loss++;
          else {
            std::vector<uint8_t> acl(data.size() + 8);
            put16(acl.data(), 1);
            put16(acl.data() + 2, static_cast<uint16_t>(data.size() + 4));
            put16(acl.data() + 4, static_cast<uint16_t>(data.size()));
            put16(acl.data() + 6, 64);
            std::memcpy(acl.data() + 8, data.data(), data.size());
            dispatcher.acl(acl.data(), static_cast<uint32_t>(acl.size()));
            if (damaged_source && sequence % 8 == 0) {
              put16(acl.data(), 3);
              put16(acl.data() + 6, 65);
              dispatcher.acl(acl.data(), static_cast<uint32_t>(acl.size()));
            }
            received++;
          }
          sequence++;
          random = random * 1664525u + 1013904223u;
          double variation = (static_cast<double>(random) / UINT32_MAX - .5) * 2 * jitter;
          packet_time = std::max(previous_arrival, sequence * packet_duration + variation);
          previous_arrival = packet_time;
        }
      }
      if (source_voice)
        api.bh_voice_destroy(source_voice);
    });
    std::thread renderer([&, this] {
      Priority priority;
      Timer timer;
      std::array<float, 960> pcm{};
      uint64_t block = 0;
      while (!replay_stop && block * .010 < duration) {
        auto due = origin + block * frequency() / 100;
        timer.until(due);
        if (ticks() > due + frequency() / 100)
          render_deadline_misses++;
        api.bh_mix_render(mixer, pcm.data(), 480, api.bh_mix_master_value(mixer));
        if (block >= 100) {
          uint64_t q = api.bh_mix_value(mixer, id, 0);
          minimum_queue = std::min(minimum_queue.load(), q);
          maximum_queue = std::max(maximum_queue.load(), q);
          for (size_t i = 0; i < 480; i++) {
            bool silent = std::abs(pcm[i * 2]) + std::abs(pcm[i * 2 + 1]) < 1e-8;
            if (silent) {
              zero_frames++;
              auto n = ++current_zero_run;
              max_zero_run = std::max(max_zero_run.load(), n);
            } else
              current_zero_run = 0;
          }
        }
        rendered += 480;
        block++;
      }
    });
    producer.join();
    renderer.join();
    json report = {
        {"done", true},
        {"scope",
         "Native synthetic HCI, codecs and PCM endpoints; no USB, playback or microphone opened"},
        {"codec", code},
        {"voice_codec", selected_voice},
        {"seconds", duration},
        {"elapsed_seconds", (ticks() - origin) / static_cast<double>(frequency())},
        {"received_packets", received.load()},
        {"injected_lost_packets", injected_loss.load()},
        {"rendered_frames", rendered.load()},
        {"zero_frames_after_warmup", zero_frames.load()},
        {"maximum_zero_run_ms", max_zero_run.load() / 48.0},
        {"render_deadline_misses", render_deadline_misses.load()},
        {"queued_ms_min", minimum_queue == UINT64_MAX ? 0 : minimum_queue.load() / 48.0},
        {"queued_ms_max", maximum_queue.load() / 48.0},
        {"underruns", api.bh_mix_value(mixer, id, 3)},
        {"prefill_frames", api.bh_mix_value(mixer, id, 4)},
        {"dropped_frames", api.bh_mix_value(mixer, id, 1)},
        {"decoded_frames", media[id]->decoded.load()},
        {"codec_errors", media[id]->errors.load()},
        {"compressed_queue_drops", media[id]->drops.load()},
        {"voice_rx_packets", voice_rx_packets.load()},
        {"voice_tx_packets", voice_tx_packets.load()},
        {"voice_plc_frames", voice_plc.load()}};
    {
      std::lock_guard<std::mutex> lock(replay_mutex);
      replay_result = std::move(report);
    }
    replaying = false;
  });
}
