/* Native audio graph and registered HCI fast paths. Apache-2.0. */
#pragma once
#include "runtime_api.hpp"
#include "runtime_ldac.hpp"
#include "runtime_usb.hpp"
#include "runtime_stream_guard.hpp"
#include <deque>
#include <future>
#include <fstream>
namespace bh {
class Core final : public HciTarget {
  bool offline = false, enhance_audio = true;
  std::atomic<bool> quiesced{false};
  std::mutex parameter_mutex;
  std::atomic<bool> replay_stop{false}, replaying{false};
  std::atomic<uint64_t> replay_started{0};
  std::thread replay_worker;
  std::mutex replay_mutex;
  json replay_result = json::object();
  std::unique_ptr<HciDispatch> test_dispatch;
  struct Media {
    Core &core;
    int input;
    BhMedia *decoder = nullptr;
    std::unique_ptr<LdacWorker> ldac;
    std::unique_ptr<float[]> ldac_pcm;
    double ldac_retry = 0;
    Spsc<Packet, 16> queue;
    Handle wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
    std::thread worker;
    std::atomic<bool> stop{false}, paused{true}, restart{false};
    std::atomic<uint32_t> handle{0xffff}, cid{0xffff}, epoch{1}, producers{0};
    uint32_t lease = 0;
    std::atomic<uint64_t> packets{0}, drops{0}, decoded{0}, errors{0}, coded{0}, lost{0},
        recoveries{0}, last_packet{0}, last_decoded{0}, concealed{0}, timeline_resets{0};
    uint32_t codec = 0, rate = 0, channels = 0;
    std::string key;
    std::mutex commit_mutex;
    StreamGuard guard;
    std::atomic<bool> allowed{true}, protected_audio{false}, guard_reset{true};
    uint64_t meter_start = 0, meter_bytes = 0;
    std::atomic<uint64_t> bitrate{0};
    explicit Media(Core &c, int id) : core(c), input(id) {}
    void retire() {
      handle = cid = 0xffff;
      while (producers.load())
        SwitchToThread();
      stop = true;
      SetEvent(wake);
      if (worker.joinable())
        worker.join();
      if (decoder) {
        core.api.bh_media_destroy(decoder);
        decoder = nullptr;
      }
      ldac.reset();
      ldac_pcm.reset();
      Packet discard;
      while (queue.pop(discard)) {
      };
    }
    ~Media() { retire(); }
    void configure(int code, int hz, int ch, const std::string &k) {
      std::unique_ptr<BhMedia, decltype(core.api.bh_media_destroy)> prepared(
          nullptr, core.api.bh_media_destroy);
      std::unique_ptr<LdacWorker> prepared_ldac;
      std::unique_ptr<float[]> prepared_pcm;
      if (code == 5) {
        prepared_ldac = std::make_unique<LdacWorker>(core.assets, hz, ch);
        prepared_pcm = std::make_unique<float[]>(48064 * 2);
      } else {
        prepared.reset(core.api.bh_media_create(code, hz, ch, 48000));
        require(prepared != nullptr, "Native media codec unavailable");
        core.api.bh_media_continuity(prepared.get(), core.enhance_audio);
      }
      retire();
      codec = code;
      rate = hz;
      channels = ch;
      key = k;
      packets = drops = decoded = errors = coded = lost = recoveries = last_packet = last_decoded =
          0;
      meter_start = meter_bytes = 0;
      bitrate = 0;
      decoder = prepared.release();
      ldac = std::move(prepared_ldac);
      ldac_pcm = std::move(prepared_pcm);
      guard_reset = true;
      protected_audio = false;
      core.api.bh_mix_clear(core.mixer, input);
      stop = false;
      paused = true;
      epoch++;
      worker = std::thread([this] { run(); });
    }
    void enqueue(uint32_t generation, const uint8_t *data, uint32_t n) {
      producers++;
      if (handle != 0xffff && !paused && generation == epoch && n && n <= 65535) {
        Packet p;
        p.size = n;
        p.epoch = epoch;
        p.arrival = ticks();
        std::memcpy(p.data.data(), data, n);
        last_packet = p.arrival;
        packets++;
        if (!queue.push(p))
          drops++;
        else
          SetEvent(wake);
      }
      producers--;
    }
    void update_guard(uint64_t arrival, uint64_t frames, uint64_t missing, uint64_t failed) {
      if (guard_reset.exchange(false))
        guard.reset();
      bool competing = false;
      auto now = ticks();
      if (allowed)
        for (const auto &other : core.media)
          if (other.get() != this && other->allowed && other->handle != 0xffff && !other->paused &&
              now - other->last_packet.load() < frequency()) {
            competing = true;
            break;
          }
      protected_audio = guard.observe(arrival / static_cast<double>(frequency()), frames, 1,
                                      missing, failed, competing);
      core.api.bh_mix_gate(core.mixer, input, allowed && !protected_audio);
    }
    void run() {
      Priority priority;
      Packet p;
      while (!stop) {
        WaitForSingleObject(wake, INFINITE);
        while (!stop && queue.pop(p)) {
          if (paused || p.epoch != epoch)
            continue;
          if (restart.exchange(false)) {
            meter_start = meter_bytes = 0;
            bitrate = 0;
            if (decoder)
              core.api.bh_media_restart(decoder);
            else
              ldac.reset();
          }
          if (codec == 5) {
            try {
              if (!ldac) {
                if (seconds() < ldac_retry)
                  continue;
                ldac = std::make_unique<LdacWorker>(core.assets, rate, channels);
              }
              uint64_t stats[10]{};
              uint32_t frames = ldac->feed(p.data.data(), p.size, ldac_pcm.get(), stats);
              std::lock_guard<std::mutex> commit(commit_mutex);
              if (!paused && p.epoch == epoch) {
                update_guard(p.arrival, frames, stats[5] >= lost ? stats[5] - lost.load() : 0,
                             stats[3] >= errors ? stats[3] - errors.load() : 0);
                if (frames) {
                  core.api.bh_mix_write(core.mixer, input, ldac_pcm.get(), frames);
                  decoded += frames;
                  last_decoded = ticks();
                }
              }
              coded = stats[2];
              update_meter(p.arrival);
              lost = stats[5];
              recoveries = stats[4];
              errors = stats[3];
            } catch (const std::exception &) {
              errors++;
              ldac.reset();
              ldac_retry = seconds() + 1;
            }
            continue;
          }
          auto before_frames = core.api.bh_media_value(decoder, 0),
               before_errors = core.api.bh_media_value(decoder, 3),
               before_lost = core.api.bh_media_value(decoder, 5);
          int result = core.api.bh_media_feed(decoder, p.data.data(), p.size);
          auto real_frames = core.api.bh_media_value(decoder, 0) - before_frames;
          auto failed = core.api.bh_media_value(decoder, 3) - before_errors;
          if (result < 0 && !failed)
            failed = 1;
          errors += failed;
          std::lock_guard<std::mutex> commit(commit_mutex);
          if (!paused && p.epoch == epoch) {
            update_guard(p.arrival, real_frames, core.api.bh_media_value(decoder, 5) - before_lost,
                         failed);
            core.api.bh_media_drain_mixer(decoder, core.mixer, input);
            decoded = core.api.bh_media_value(decoder, 0);
            if (real_frames)
              last_decoded = ticks();
          } else
            core.api.bh_media_discard(decoder);
          coded = core.api.bh_media_value(decoder, 2);
          update_meter(p.arrival);
          lost = core.api.bh_media_value(decoder, 5);
          concealed = core.api.bh_media_value(decoder, 10);
          timeline_resets = core.api.bh_media_value(decoder, 11);
          recoveries = core.api.bh_media_value(decoder, 4);
        }
      }
    }
    void update_meter(uint64_t now) {
      auto bytes = coded.load();
      if (!meter_start || bytes < meter_bytes) {
        meter_start = now;
        meter_bytes = bytes;
        return;
      }
      if (now - meter_start >= frequency() / 2) {
        bitrate = (bytes - meter_bytes) * 8 * frequency() / (now - meter_start);
        meter_start = now;
        meter_bytes = bytes;
      }
    }
    json snapshot() {
      return {{"codec", codec},
              {"rate", rate},
              {"packets", packets.load()},
              {"dropped_packets", drops.load()},
              {"decoded_frames", decoded.load()},
              {"decode_errors", errors.load()},
              {"coded_bytes", coded.load()},
              {"bitrate",
               !paused && ticks() - last_packet.load() < frequency() * 3 / 2 ? bitrate.load() : 0},
              {"lost_packets", lost.load()},
              {"concealed_frames", concealed.load()},
              {"timeline_resets", timeline_resets.load()},
              {"recoveries", recoveries.load()},
              {"protected", protected_audio.load()},
              {"playing", !paused && ticks() - last_decoded.load() < frequency() * 3 / 2},
              {"buffer_ms", core.api.bh_mix_value(core.mixer, input, 0) / 48.0},
              {"last_packet_age_ms",
               last_packet ? (ticks() - last_packet.load()) * 1000.0 / frequency() : 1e12}};
    }
  };
  struct Wire {
    uint32_t size = 0, epoch = 0;
    std::array<uint8_t, 120> bytes{};
  };
  struct AudioJob {
    uint64_t id = 0;
    std::string direction, endpoint;
    bool start = true;
    uint32_t voice_generation = 0;
    bool simulated = false;
    uint32_t delay_ms = 0;
  };
  std::array<std::unique_ptr<Media>, 8> media;
  std::map<std::string, int> keys;
  std::unique_ptr<Usb> usb;
  void *mixer = nullptr, *uplink = nullptr;
  std::atomic<bool> quitting{false}, voice_stop{true}, voice_rx_enabled{false};
  std::atomic<uint32_t> voice_handle{0xffff}, voice_codec{0}, voice_epoch{1}, voice_producers{0},
      voice_consumers{0};
  std::atomic<int> voice_input{-1};
  Spsc<SmallPacket, 128> voice_rx;
  Spsc<Wire, 16> voice_tx;
  Wire tx_partial;
  uint32_t tx_offset = 0, tx_seen_epoch = 0;
  std::thread voice_worker, bridge_worker, audio_worker;
  std::thread watchdog_worker;
  std::atomic<bool> watchdog_stop{false};
  std::atomic<uint64_t> audio_deadline{0};
  Handle watchdog_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  Handle voice_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  Handle bridge_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  bh_voice *voice = nullptr;
  std::atomic<uint64_t> voice_rx_packets{0}, voice_tx_packets{0}, voice_plc{0},
      voice_decode_errors{0}, voice_rx_bytes{0}, voice_tx_bytes{0}, voice_frames{0};
  std::atomic<float> microphone_gain{1}, system_gain{1}, device_gain{1};
  std::atomic<float> playback_gain{1};
  std::array<uint64_t, 3> master_revisions{};
  uint64_t mute_revision = 0;
  std::atomic<bool> microphone_muted{false}, mix_enabled{false}, single{false};
  std::atomic<int> media_owner{-1};
  std::atomic<uint64_t> bridge_frames{0}, system_frames{0}, bridge_errors{0};
  std::string voice_key;
  std::mutex bridge_mutex;
  BhDeviceContext *context = nullptr;
  BhAudioDevice *output = nullptr, *capture = nullptr;
  void *loop_capture = nullptr, *loop_converter = nullptr;
  std::string output_selection, input_selection;
  bool output_wanted = false, capture_wanted = false;
  std::mutex audio_mutex;
  std::condition_variable audio_condition;
  std::deque<AudioJob> audio_jobs;
  std::map<uint64_t, json> audio_results;
  uint64_t next_job = 0;
  std::array<uint64_t, 3> latest_job{};
  std::atomic<uint64_t> device_reopens{0};
  std::atomic<bool> microphone_active{false}, system_active{false}, output_active{false};
  json device_error = json::object();
  uint32_t media_buffer = 2880, voice_buffer = 1440;
  fs::path assets;
  int key_input(const std::string &key) {
    auto found = keys.find(key);
    require(found != keys.end(), "Unknown audio source");
    return found->second;
  }
  static int direction_id(const std::string &s) { return s == "output" ? 0 : s == "input" ? 1 : 2; }
  uint64_t schedule(const std::string &direction, const std::string &endpoint, bool start = true,
                    uint32_t generation = 0, bool simulated = false, uint32_t delay_ms = 0,
                    uint64_t expected_latest = UINT64_MAX) {
    std::lock_guard<std::mutex> lock(audio_mutex);
    if (expected_latest != UINT64_MAX && expected_latest != latest_job[direction_id(direction)])
      return 0;
    uint64_t id = ++next_job;
    latest_job[direction_id(direction)] = id;
    for (auto it = audio_jobs.begin(); it != audio_jobs.end();) {
      if (it->direction == direction) {
        audio_results[it->id] = {{"done", true}, {"success", false}, {"cancelled", true}};
        it = audio_jobs.erase(it);
      } else
        ++it;
    }
    while (audio_results.size() > 64)
      audio_results.erase(audio_results.begin());
    audio_jobs.push_back({id, direction, endpoint, start, generation, simulated, delay_ms});
    audio_condition.notify_one();
    return id;
  }
  void finish(const AudioJob &j, const json &result) {
    std::lock_guard<std::mutex> lock(audio_mutex);
    audio_results[j.id] = result;
    while (audio_results.size() > 64)
      audio_results.erase(audio_results.begin());
  }
  void audio_loop() {
    HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    double next_check = seconds() + .5;
    while (!quitting) {
      AudioJob job;
      bool have = false;
      {
        std::unique_lock<std::mutex> lock(audio_mutex);
        audio_condition.wait_for(lock, std::chrono::milliseconds(100),
                                 [this] { return quitting || !audio_jobs.empty(); });
        if (!audio_jobs.empty()) {
          job = std::move(audio_jobs.front());
          audio_jobs.pop_front();
          have = true;
        }
      }
      if (have) {
        audio_deadline = ticks() + frequency() * 12;
        try {
          execute_audio(job);
        } catch (const std::exception &e) {
          finish(job, {{"done", true}, {"success", false}, {"error", e.what()}});
        }
        audio_deadline = 0;
      }
      if (seconds() >= next_check) {
        next_check = seconds() + .5;
        audio_deadline = ticks() + frequency() * 12;
        try {
          check_devices();
        } catch (const std::exception &) {
          bridge_errors++;
        }
        audio_deadline = 0;
      }
    }
    audio_deadline = ticks() + frequency() * 12;
    {
      std::lock_guard<std::mutex> lock(bridge_mutex);
      if (capture) {
        api.bh_device_close(capture);
        capture = nullptr;
      }
      if (loop_capture) {
        if (api.bh_loop_close(loop_capture) != 1)
          TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
        loop_capture = nullptr;
      }
      if (loop_converter) {
        api.bh_capture_free(loop_converter);
        loop_converter = nullptr;
      }
    }
    if (output) {
      api.bh_device_close(output);
      output = nullptr;
    }
    microphone_active = system_active = output_active = false;
    // miniaudio's WASAPI context and its COM references are retired by the
    // same MTA owner that initialized them, before this thread leaves COM.
    if (context) {
      if (api.bh_device_context_close(context) != 0)
        TerminateProcess(GetCurrentProcess(), ERROR_BUSY);
      context = nullptr;
    }
    audio_deadline = 0;
    if (SUCCEEDED(apartment))
      CoUninitialize();
  }
  void execute_audio(const AudioJob &job) {
    {
      std::lock_guard<std::mutex> lock(audio_mutex);
      if (latest_job[direction_id(job.direction)] != job.id ||
          (job.voice_generation && job.voice_generation != voice_epoch)) {
        audio_results[job.id] = {{"done", true}, {"success", false}, {"cancelled", true}};
        return;
      }
    }
    if (job.direction == "system") {
      void *candidate = nullptr, *converter = nullptr;
      DWORD error = 0;
      if (job.start) {
        candidate = api.bh_loop_open(GetCurrentProcessId(), &error);
        require(candidate, "Computer audio capture unavailable");
        api.bh_loop_ready_event(candidate, bridge_wake);
        converter = api.bh_capture_create(48000, 1, 48000);
        if (!converter) {
          api.bh_loop_close(candidate);
          throw std::runtime_error("Computer audio converter unavailable");
        }
      }
      bool cancelled = false;
      std::unique_lock<std::mutex> commit_lock(audio_mutex);
      {
        cancelled = latest_job[2] != job.id ||
                    (job.voice_generation && job.voice_generation != voice_epoch);
      }
      if (cancelled) {
        commit_lock.unlock();
        if (candidate)
          api.bh_loop_close(candidate);
        if (converter)
          api.bh_capture_free(converter);
        finish(job, {{"done", true}, {"success", false}, {"cancelled", true}});
        return;
      }
      void *old = nullptr, *old_converter = nullptr;
      {
        std::lock_guard<std::mutex> lock(bridge_mutex);
        old = loop_capture;
        old_converter = loop_converter;
        loop_capture = candidate;
        loop_converter = converter;
        api.bh_up_clear(uplink, 1);
      }
      audio_results[job.id] = {{"done", true}, {"success", true}};
      commit_lock.unlock();
      if (old && api.bh_loop_close(old) != 1)
        TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
      if (old_converter)
        api.bh_capture_free(old_converter);
      system_active = job.start;
      finish(job, {{"done", true}, {"success", true}});
      return;
    }
    bool render = job.direction == "output";
    int error = 0;
    BhAudioDevice *candidate = nullptr;
    auto endpoint = wide(job.endpoint);
    if (job.simulated) {
      require(offline, "Offline-test mode required");
      Sleep(job.delay_ms);
      if (job.start)
        candidate = api.bh_device_offline(context, render ? BH_PLAYBACK : BH_CAPTURE, mixer, 48000,
                                          render ? 2 : 1, 48000, &error);
      require(!job.start || candidate, "Offline endpoint preparation failed");
    } else if (job.start) {
      candidate =
          render ? api.bh_device_open_output(context, mixer,
                                             endpoint.empty() ? nullptr : endpoint.c_str(), &error)
                 : api.bh_device_open_capture(
                       context, endpoint.empty() ? nullptr : endpoint.c_str(), 48000, &error);
      require(candidate,
              render ? "Cannot prepare playback endpoint" : "Cannot prepare microphone endpoint");
    }
    if (candidate && !render)
      api.bh_device_ready_event(candidate, bridge_wake);
    bool cancelled = false;
    std::unique_lock<std::mutex> commit_lock(audio_mutex);
    {
      cancelled = latest_job[render ? 0 : 1] != job.id ||
                  (job.voice_generation && job.voice_generation != voice_epoch);
    }
    if (cancelled) {
      commit_lock.unlock();
      if (candidate)
        api.bh_device_close(candidate);
      finish(job, {{"done", true}, {"success", false}, {"cancelled", true}});
      return;
    }
    BhAudioDevice *old = nullptr;
    if (render) {
      old = output;
      if (old && api.bh_device_stop(old) != 0) {
        if (candidate)
          api.bh_device_close(candidate);
        throw std::runtime_error(
            "Previous playback endpoint did not stop; new consumer was not started");
      }
      if (candidate && api.bh_device_start(candidate) != 0) {
        if (old)
          api.bh_device_start(old);
        api.bh_device_close(candidate);
        throw std::runtime_error("New playback endpoint could not start");
      }
      output = candidate;
      output_active = job.start;
      output_selection = job.endpoint;
      output_wanted = job.start;
    } else {
      if (candidate && api.bh_device_start(candidate) != 0) {
        api.bh_device_close(candidate);
        throw std::runtime_error("New microphone endpoint could not start");
      }
      {
        std::lock_guard<std::mutex> lock(bridge_mutex);
        old = capture;
        capture = candidate;
        api.bh_up_clear(uplink, 0);
        microphone_active = job.start;
      }
      input_selection = job.endpoint;
      capture_wanted = job.start;
    }
    audio_results[job.id] = {{"done", true}, {"success", true}, {"endpoint_id", job.endpoint}};
    commit_lock.unlock();
    if (old)
      api.bh_device_close(old);
    finish(job, {{"done", true}, {"success", true}, {"endpoint_id", job.endpoint}});
  }
  std::string default_output, default_input;
  bool inventory_known = false;
  void check_devices() {
    if (offline)
      return;
    output_active = output && api.bh_device_active(output);
    microphone_active = capture && api.bh_device_active(capture);
    if (!output_wanted && !capture_wanted)
      return;
    uint32_t count = 0;
    int result = api.bh_device_inventory(context, nullptr, 0, &count);
    if (result != 0 && result != BH_DEVICE_BUFFER_SMALL)
      return;
    if (count > 4096)
      return;
    std::vector<BhEndpoint> endpoints(count);
    if (api.bh_device_inventory(context, endpoints.data(), count, &count) != 0)
      return;
    std::string out, in;
    bool output_present = output_selection.empty(), input_present = input_selection.empty();
    for (const auto &e : endpoints) {
      auto identity = utf8(e.endpoint_id);
      if (e.direction == BH_PLAYBACK && identity == output_selection)
        output_present = true;
      if (e.direction == BH_CAPTURE && identity == input_selection)
        input_present = true;
      if (e.is_default) {
        if (e.direction == BH_PLAYBACK)
          out = utf8(e.endpoint_id);
        else
          in = utf8(e.endpoint_id);
      }
    }
    bool output_changed = inventory_known && output_selection.empty() && out != default_output;
    bool input_changed = inventory_known && input_selection.empty() && in != default_input;
    default_output = out;
    default_input = in;
    inventory_known = true;
    bool pending_output = false, pending_input = false;
    uint64_t output_ticket = 0, input_ticket = 0;
    {
      std::lock_guard<std::mutex> lock(audio_mutex);
      output_ticket = latest_job[0];
      input_ticket = latest_job[1];
      pending_output = std::any_of(audio_jobs.begin(), audio_jobs.end(),
                                   [](const auto &j) { return j.direction == "output"; });
      pending_input = std::any_of(audio_jobs.begin(), audio_jobs.end(),
                                  [](const auto &j) { return j.direction == "input"; });
    }
    if (output_wanted && !pending_output &&
        (!output || !api.bh_device_active(output) || output_changed || !output_present)) {
      device_reopens++;
      schedule("output", output_present ? output_selection : "", true, 0, false, 0, output_ticket);
    }
    if (capture_wanted && !pending_input &&
        (!capture || !api.bh_device_active(capture) || input_changed || !input_present)) {
      device_reopens++;
      schedule("input", input_present ? input_selection : "", true, 0, false, 0, input_ticket);
    }
  }
  void bridge_loop() {
    Priority priority;
    std::array<int16_t, 2048> pcm{};
    std::array<float, 4800> system{};
    while (!quitting) {
      {
        std::lock_guard<std::mutex> lock(bridge_mutex);
        if (capture) {
          for (unsigned batch = 0; batch < 8; batch++) {
            int n = api.bh_device_read(capture, pcm.data(), 1024);
            if (n <= 0) {
              if (n < 0)
                bridge_errors++;
              break;
            }
            if (voice_handle != 0xffff)
              api.bh_up_write(uplink, 0, reinterpret_cast<const uint8_t *>(pcm.data()), n * 2);
            bridge_frames += n;
            // A large endpoint callback may contain several read quanta.
            // Re-arm a bounded drain rather than wait for another callback.
            if (batch == 7)
              SetEvent(bridge_wake);
          }
        }
        if (loop_capture && loop_converter) {
          int n = api.bh_loop_read(loop_capture, system.data(), 4800);
          if (n > 0) {
            api.bh_capture_push(loop_converter, system.data(), n);
            while ((n = api.bh_capture_read(loop_converter, pcm.data(), 1024)) > 0) {
              if (voice_handle != 0xffff && mix_enabled)
                api.bh_up_write(uplink, 1, reinterpret_cast<const uint8_t *>(pcm.data()), n * 2);
              system_frames += n;
            }
          }
        }
      }
      SetEvent(voice_wake);
      WaitForSingleObject(bridge_wake, INFINITE);
    }
  }
  void voice_loop() {
    Priority priority;
    SmallPacket p;
    Wire wire;
    std::array<int16_t, 11520> pcm{};
    Timer timer;
    uint32_t consecutive_missing = 0;
    uint64_t expected = 0;
    uint64_t frame_ticks = frequency() * 75 / 10000;
    while (!voice_stop) {
      if (voice_handle == 0xffff || !voice_rx_enabled)
        expected = 0;
      unsigned batch = 0;
      while (batch++ < 64 && voice_rx.pop(p)) {
        if (p.epoch != voice_epoch || voice_handle == 0xffff)
          continue;
        int frames = api.bh_voice_push_rx(voice, p.data.data(), p.size, p.status);
        if (frames < 0) {
          voice_decode_errors++;
          continue;
        }
        if (frames > 0) {
          if (!expected || p.arrival > expected + frame_ticks * 16)
            expected = p.arrival;
          expected = std::max(expected + frame_ticks * frames, p.arrival + frame_ticks);
          consecutive_missing = 0;
        }
        uint32_t n = api.bh_voice_read_pcm(voice, pcm.data(), pcm.size());
        if (n && voice_input >= 0) {
          api.bh_mix_write_s16_mono(mixer, voice_input, pcm.data(), n);
          voice_frames += n;
        }
        bh_voice_stats stats{};
        api.bh_voice_get_stats(voice, &stats, sizeof(stats));
        voice_plc = stats.plc_frames;
      }
      auto now = ticks();
      if (voice_rx_enabled && expected && now > expected + frame_ticks && voice_rx.size() == 0) {
        uint32_t missed = static_cast<uint32_t>(
            std::min<uint64_t>(8 - consecutive_missing, (now - expected) / frame_ticks));
        if (missed) {
          api.bh_voice_conceal(voice, missed);
          uint32_t n = api.bh_voice_read_pcm(voice, pcm.data(), pcm.size());
          if (n && voice_input >= 0)
            api.bh_mix_write_s16_mono(mixer, voice_input, pcm.data(), n);
          expected += frame_ticks * missed;
          voice_plc += missed;
          consecutive_missing += missed;
          if (consecutive_missing == 8)
            expected = 0;
        }
      }
      while (!voice_stop && voice_handle != 0xffff && voice_tx.size() < 2) {
        api.bh_up_gain(uplink, microphone_gain.load(), system_gain.load(),
                       microphone_muted ? 0.0f : device_gain.load(), microphone_muted.load());
        if (api.bh_up_read(uplink, reinterpret_cast<uint8_t *>(pcm.data()), 720) >= 0) {
          wire.epoch = voice_epoch;
          int n = api.bh_voice_encode(voice, pcm.data(), 360, wire.bytes.data(), wire.bytes.size());
          if (n > 0) {
            wire.size = n;
            voice_tx.push(wire);
          } else {
            voice_decode_errors++;
            break;
          }
        }
      }
      if (expected)
        timer.wait(expected + frame_ticks, voice_wake);
      else
        WaitForSingleObject(voice_wake, INFINITE);
    }
  }
  void stop_voice() {
    voice_handle = 0xffff;
    voice_rx_enabled = false;
    while (voice_producers.load() || voice_consumers.load())
      SwitchToThread();
    voice_stop = true;
    SetEvent(voice_wake);
    if (voice_worker.joinable())
      voice_worker.join();
    if (voice) {
      api.bh_voice_destroy(voice);
      voice = nullptr;
    }
    SmallPacket p;
    while (voice_rx.pop(p)) {
    };
    Wire w;
    while (voice_tx.pop(w)) {
    };
    voice_codec = 0;
    voice_epoch++;
    api.bh_mix_call(mixer, -1);
  }

public:
  Api api;
  explicit Core(const fs::path &path, bool offline_test = false)
      : offline(offline_test), assets(path), api(path) {
    api.bh_media_log_quiet();
    mixer = api.bh_mix_create();
    uplink = api.bh_up_create();
    require(mixer && uplink, "Native audio graph allocation failed");
    api.bh_up_rate(uplink, 48000);
    api.bh_up_adaptive(uplink, 1);
    api.bh_up_continuity(uplink, 1);
    int error = 0;
    context = api.bh_device_context_create((assets / L"bluehead_audio.dll").c_str(), &error);
    require(context, "WASAPI context could not initialize");
    for (int i = 0; i < 8; i++)
      media[i] = std::make_unique<Media>(*this, i);
    watchdog_worker = std::thread([this] {
      while (!watchdog_stop) {
        WaitForSingleObject(watchdog_wake, 100);
        auto deadline = audio_deadline.load();
        if (deadline && ticks() > deadline)
          TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
      }
    });
    audio_worker = std::thread([this] { audio_loop(); });
    bridge_worker = std::thread([this] { bridge_loop(); });
  }
  ~Core() {
    replay_stop = true;
    if (replay_worker.joinable())
      replay_worker.join();
    if (usb) {
      usb->close();
      usb.reset();
    }
    stop_voice();
    for (auto &m : media)
      m->retire();
    quitting = true;
    SetEvent(bridge_wake);
    audio_condition.notify_all();
    if (bridge_worker.joinable())
      bridge_worker.join();
    if (audio_worker.joinable())
      audio_worker.join();
    watchdog_stop = true;
    SetEvent(watchdog_wake);
    if (watchdog_worker.joinable())
      watchdog_worker.join();
    for (auto &m : media)
      m.reset();
    if (context)
      api.bh_device_context_close(context);
    api.bh_up_free(uplink);
    api.bh_mix_free(mixer);
  }
  uint32_t media_route(uint16_t handle, uint16_t cid) override {
    for (const auto &m : media)
      if (m->handle == handle && m->cid == cid)
        return m->epoch.load();
    return false;
  }
  void media_packet(uint16_t handle, uint16_t cid, uint32_t generation, const uint8_t *p,
                    uint32_t n) override {
    for (auto &m : media)
      if (m->handle == handle && m->cid == cid) {
        m->enqueue(generation, p, n);
        return;
      }
  }
  bool voice_packet(uint16_t handle, uint32_t status, const uint8_t *p, uint32_t n) override {
    voice_producers++;
    bool accepted = voice_handle == handle;
    if (accepted && n && n <= 255) {
      SmallPacket packet;
      packet.size = n;
      packet.status = status;
      packet.epoch = voice_epoch;
      packet.arrival = ticks();
      std::memcpy(packet.data.data(), p, n);
      if (!voice_rx.push(packet))
        voice_decode_errors++;
      else {
        voice_rx_enabled = true;
        voice_rx_packets++;
        voice_rx_bytes += n;
        SetEvent(voice_wake);
      }
    }
    voice_producers--;
    return accepted;
  }
  uint32_t voice_byte_rate() const override {
    auto codec = voice_codec.load();
    return quiesced || voice_handle == 0xffff ? 0 : codec == 1 ? 16000 : codec ? 8000 : 0;
  }
  uint32_t voice_output(uint16_t &handle, uint8_t *out, uint32_t capacity) override {
    CounterGuard guard(voice_consumers);
    SetEvent(voice_wake);
    auto epoch = voice_epoch.load();
    handle = static_cast<uint16_t>(voice_handle.load());
    if (quiesced || handle == 0xffff || capacity > 255)
      return 0;
    if (tx_seen_epoch != epoch) {
      tx_seen_epoch = epoch;
      tx_partial.size = tx_offset = 0;
    }
    uint32_t frame = voice_codec == 1 ? 120 : 60;
    if ((tx_partial.size - tx_offset) + voice_tx.size() * frame < capacity)
      return 0;
    uint32_t written = 0;
    while (written < capacity) {
      if (tx_offset == tx_partial.size) {
        if (!voice_tx.pop(tx_partial))
          return 0;
        tx_offset = 0;
        if (tx_partial.epoch != epoch) {
          tx_partial.size = 0;
          return 0;
        }
      }
      uint32_t take = std::min(capacity - written, tx_partial.size - tx_offset);
      std::memcpy(out + written, tx_partial.bytes.data() + tx_offset, take);
      written += take;
      tx_offset += take;
    }
    voice_tx_packets++;
    voice_tx_bytes += written;
    SetEvent(voice_wake);
    return written;
  }
  void disconnected(uint16_t handle) override {
    for (auto &m : media)
      if (m->handle == handle) {
        m->paused = true;
        m->epoch++;
        m->handle = 0xffff;
      }
    if (voice_handle == handle) {
      voice_handle = 0xffff;
      voice_rx_enabled = false;
      api.bh_mix_call(mixer, -1);
      if (voice_input >= 0)
        api.bh_mix_gate(mixer, voice_input, false);
      SetEvent(voice_wake);
    }
  }
  void transport_lost() noexcept override {
    for (auto &m : media) {
      m->paused = true;
      m->epoch++;
      m->handle = 0xffff;
      api.bh_mix_gate(mixer, m->input, false);
    }
    voice_handle = 0xffff;
    voice_rx_enabled = false;
    api.bh_mix_call(mixer, -1);
    SetEvent(voice_wake);
  }
  void quiesce_now() {
    std::lock_guard<std::mutex> lock(parameter_mutex);
    quiesced = true;
    api.bh_mix_master(mixer, 0);
  }
  json command(const json &request, const fs::path &assets) {
    std::string op = request.at("op").get<std::string>();
    if (offline && (op == "usb_open" || op == "audio_switch" || op == "system_audio"))
      throw std::runtime_error("Hardware opening is disabled in offline-test mode");
    if (request.contains("lease")) {
      auto id = key_input(request.at("key"));
      require(request.at("lease").get<uint32_t>() == media[id]->lease,
              "Audio source session has expired");
    }
    if (op == "test_replay") {
      start_replay(request);
      return true;
    }
    if (op == "test_replay_status") {
      require(offline, "Offline-test mode required");
      std::lock_guard<std::mutex> lock(replay_mutex);
      auto result = replay_result;
      result["running"] = replaying.load();
      if (replaying)
        result["elapsed_seconds"] =
            (ticks() - replay_started.load()) / static_cast<double>(frequency());
      return result;
    }
    if (op == "test_replay_stop") {
      require(offline, "Offline-test mode required");
      replay_stop = true;
      if (replay_worker.joinable())
        replay_worker.join();
      return true;
    }
    if (op == "test_hci_acl") {
      require(offline && !replaying, "Exclusive offline-test mode required");
      auto data = unhex(request.at("packet"));
      if (!test_dispatch)
        test_dispatch = std::make_unique<HciDispatch>(*this);
      bool handled = test_dispatch->acl(data.data(), data.size());
      return {{"handled", handled}, {"malformed", test_dispatch->malformed}};
    }
    if (op == "test_completions") {
      require(offline, "Offline-test mode required");
      auto packet = unhex(request.at("packet"), 257);
      auto handles = request.at("sco_handles").get<std::vector<uint16_t>>();
      require(handles.size() <= 3, "Too many SCO handles");
      std::array<uint8_t, 257> filtered{};
      uint32_t length = 0;
      uint64_t completed = 0;
      bool handled = filter_completions(
          packet.data(), packet.size(), filtered, length,
          [&](uint16_t h) { return std::find(handles.begin(), handles.end(), h) != handles.end(); },
          completed);
      return {{"handled", handled},
              {"packet", handled && length ? hex(filtered.data(), length) : ""},
              {"sco_completed", completed}};
    }
    if (op == "test_stream_guard") {
      require(offline, "Offline-test mode required");
      const auto &events = request.at("events");
      require(events.is_array() && events.size() <= 512, "Guard input too large");
      StreamGuard guard;
      json states = json::array();
      for (const auto &event : events)
        states.push_back(guard.observe(event.at("time"), event.at("frames"), event.at("packets"),
                                       event.at("lost"), event.at("errors"),
                                       event.at("competing")));
      return states;
    }
    if (op == "test_audio_switch") {
      require(offline, "Offline-test mode required");
      std::string direction = request.at("direction");
      auto delay = request.value("delay_ms", 0u);
      require((direction == "input" || direction == "output") && delay <= 2000,
              "Invalid offline audio job");
      return schedule(direction, request.value("endpoint_id", std::string()),
                      request.value("start", true), request.value("voice_epoch", 0u), true, delay);
    }
    if (op == "test_voice_rx") {
      require(offline && !replaying, "Exclusive offline-test mode required");
      auto data = unhex(request.at("packet"), 255);
      return voice_packet(request.at("handle"), request.value("status", 0u), data.data(),
                          data.size());
    }
    if (op == "test_voice_tx") {
      require(offline && !replaying, "Exclusive offline-test mode required");
      std::array<uint8_t, 255> bytes{};
      uint16_t handle = 0;
      auto count = voice_output(handle, bytes.data(), request.value("capacity", 60u));
      return {{"handle", handle}, {"packet", hex(bytes.data(), count)}};
    }
    if (op == "test_disconnect") {
      require(offline, "Offline-test mode required");
      disconnected(request.at("handle"));
      return true;
    }
    if (op == "test_capture") {
      require(offline, "Offline-test mode required");
      uint32_t frames = request.value("frames", 480u);
      require(frames && frames <= 4800, "Invalid offline capture quantum");
      std::vector<float> pcm(frames);
      for (uint32_t i = 0; i < frames; i++)
        pcm[i] = .2f * std::sin(i * 6.283185307179586 * 1000 / 48000);
      std::lock_guard<std::mutex> lock(bridge_mutex);
      require(capture, "Offline microphone is not prepared");
      require(api.bh_device_offline_process(capture, pcm.data(), nullptr, frames) == 0,
              "Offline capture callback failed");
      return true;
    }
    if (op == "test_render") {
      require(offline && !replaying, "Exclusive offline-test mode required");
      uint32_t frames = request.value("frames", 480u);
      require(frames > 0 && frames <= 48064, "Invalid render quantum");
      std::vector<float> pcm(frames * 2);
      api.bh_mix_render(mixer, pcm.data(), frames, api.bh_mix_master_value(mixer));
      double energy = 0, peak = 0;
      for (float value : pcm) {
        require(std::isfinite(value), "Nonfinite native render");
        energy += value * value;
        peak = std::max(peak, std::abs(static_cast<double>(value)));
      }
      return {{"frames", frames}, {"rms", std::sqrt(energy / (frames * 2))}, {"peak", peak}};
    }
    if (op == "hello")
      return {{"version", version}, {"abi", 1}, {"native_usb", true}, {"native_pcm_bridge", true}};
    if (op == "quiesce") {
      quiesce_now();
      stop_voice();
      for (auto &m : media) {
        m->paused = true;
        m->epoch++;
      }
      schedule("input", "", false);
      schedule("system", "", false);
      schedule("output", "", false);
      return true;
    }
    if (op == "usb_open") {
      require(!usb, "USB owner already running");
      auto candidate = std::make_unique<Usb>(assets, *this);
      candidate->open(request.at("vid"), request.at("pid"), request.value("index", 0u));
      usb = std::move(candidate);
      {
        std::lock_guard<std::mutex> lock(parameter_mutex);
        quiesced = false;
        api.bh_mix_master(mixer, playback_gain);
      }
      return true;
    }
    if (op == "usb_close") {
      if (usb) {
        usb->close();
        usb.reset();
      }
      return true;
    }
    if (op == "usb_send") {
      auto p = unhex(request.at("packet"), 4096);
      require(usb && usb->send(p.data(), p.size()), "Native HCI transmit queue unavailable");
      return true;
    }
    if (op == "usb_poll") {
      json packets = json::array();
      SmallPacket p;
      if (usb)
        while (packets.size() < 64 && usb->pop(p))
          packets.push_back(hex(p.data.data(), p.size));
      return {{"packets", packets}, {"active", usb && usb->active()}};
    }
    if (op == "mix_config") {
      auto mb = request.value("buffer_ms", 60u), vb = request.value("call_buffer_ms", 30u);
      require(mb <= 180 && vb <= 90, "Buffer bound exceeded");
      media_buffer = mb * 48;
      voice_buffer = vb * 48;
      return true;
    }
    if (op == "mix_add") {
      auto key = request.at("key").get<std::string>();
      if (keys.count(key))
        return {{"index", keys[key]}, {"lease", media[keys[key]]->lease}};
      bool call = key.rfind("call:", 0) == 0;
      auto id = api.bh_mix_add(mixer, call ? voice_buffer : media_buffer, call ? 4320 : 8640);
      require(id >= 0, "Native mixer source capacity exceeded");
      keys[key] = id;
      media[id]->key = key;
      media[id]->lease++;
      media[id]->allowed = true;
      media[id]->protected_audio = false;
      media[id]->guard_reset = true;
      api.bh_mix_continuity(mixer, id, enhance_audio,
                            call ? std::min(voice_buffer, 960u) : std::min(media_buffer, 1440u),
                            call ? 2880 : 5760);
      return {{"index", id}, {"lease", media[id]->lease}};
    }
    if (op == "mix_remove") {
      auto key = request.at("key").get<std::string>();
      auto found = keys.find(key);
      if (found != keys.end()) {
        int id = found->second;
        media[id]->retire();
        api.bh_mix_remove(mixer, id);
        keys.erase(found);
      }
      return true;
    }
    if (op == "mix_clear") {
      api.bh_mix_clear(mixer, key_input(request.at("key")));
      return true;
    }
    if (op == "mix_gain") {
      float gain = request.at("gain");
      require(std::isfinite(gain) && gain >= 0 && gain <= 2, "Invalid source gain");
      api.bh_mix_gain(mixer, key_input(request.at("key")), gain);
      return true;
    }
    if (op == "mix_gate") {
      auto id = key_input(request.at("key"));
      auto &slot = *media[id];
      std::lock_guard<std::mutex> commit(slot.commit_mutex);
      slot.allowed = request.at("enabled").get<bool>();
      slot.guard_reset = true;
      slot.protected_audio = false;
      api.bh_mix_gate(mixer, id, slot.allowed);
      return true;
    }
    if (op == "master_gain") {
      std::lock_guard<std::mutex> lock(parameter_mutex);
      std::string bus = request.at("bus");
      float gain = request.at("gain");
      require(std::isfinite(gain) && gain >= 0 && gain <= 2, "Invalid gain");
      int index = bus == "playback" ? 0 : bus == "microphone" ? 1 : bus == "system" ? 2 : -1;
      require(index >= 0, "Invalid gain bus");
      auto revision = request.value("revision", uint64_t(0));
      if (revision < master_revisions[index])
        return true;
      master_revisions[index] = revision;
      if (bus == "playback") {
        playback_gain = gain;
        api.bh_mix_master(mixer, quiesced ? 0 : gain);
      } else if (bus == "microphone")
        microphone_gain = gain;
      else if (bus == "system")
        system_gain = gain;
      else
        throw std::runtime_error("Invalid gain bus");
      return true;
    }
    if (op == "mute") {
      std::lock_guard<std::mutex> lock(parameter_mutex);
      auto revision = request.value("revision", uint64_t(0));
      if (revision >= mute_revision) {
        microphone_muted = request.at("muted").get<bool>();
        mute_revision = revision;
      }
      return true;
    }
    if (op == "router_gain") {
      std::lock_guard<std::mutex> lock(parameter_mutex);
      if (voice_handle != 0xffff && request.value("key", voice_key) != voice_key)
        return true;
      for (auto key : {"device", "microphone", "system"}) {
        float gain = request.value(key, 1.0f);
        require(std::isfinite(gain) && gain >= 0 && gain <= 2, "Invalid uplink gain");
      }
      device_gain = request.value("device", 1.0f);
      // Initial/control-only updates cannot overwrite a newer native UI action
      // while the Python sidecar catches up after a stall.
      if (!master_revisions[1])
        microphone_gain = request.value("microphone", 1.0f);
      if (!master_revisions[2])
        system_gain = request.value("system", 1.0f);
      if (!mute_revision)
        microphone_muted = request.value("muted", false);
      return true;
    }
    if (op == "media_config") {
      int id = key_input(request.at("key"));
      media[id]->configure(request.at("codec"), request.at("rate"), request.at("channels"),
                           request.at("key"));
      return true;
    }
    if (op == "media_route") {
      auto &m = *media[key_input(request.at("key"))];
      auto handle = request.at("handle").get<uint32_t>(), cid = request.at("cid").get<uint32_t>();
      require(handle <= 0xfff && cid >= 0x40 && cid <= 0xffff, "Invalid media route");
      m.handle = 0xffff;
      m.epoch++;
      m.cid = cid;
      m.handle = handle;
      return true;
    }
    if (op == "media_unroute") {
      auto &m = *media[key_input(request.at("key"))];
      std::lock_guard<std::mutex> commit(m.commit_mutex);
      m.handle = m.cid = 0xffff;
      m.epoch++;
      m.paused = true;
      api.bh_mix_clear(mixer, m.input);
      return true;
    }
    if (op == "media_pause") {
      auto &m = *media[key_input(request.at("key"))];
      std::lock_guard<std::mutex> commit(m.commit_mutex);
      m.paused = request.at("paused").get<bool>();
      m.epoch++;
      if (!m.paused)
        m.restart = true;
      if (m.paused)
        api.bh_mix_clear(mixer, m.input);
      m.guard_reset = true;
      m.protected_audio = false;
      api.bh_mix_gate(mixer, m.input, m.allowed);
      return true;
    }
    if (op == "media_snapshot")
      return media[key_input(request.at("key"))]->snapshot();
    if (op == "voice_start") {
      require(!quiesced, "Receiver is stopping");
      require(offline || (usb && usb->active()), "Bluetooth transport is unavailable");
      auto key = request.at("key").get<std::string>();
      int id = key_input("call:" + key);
      uint32_t codec = request.at("codec");
      auto handle = request.at("handle").get<uint32_t>();
      require(handle <= 0xfff, "Invalid voice handle");
      std::unique_ptr<bh_voice, decltype(api.bh_voice_destroy)> prepared(api.bh_voice_create(codec),
                                                                         api.bh_voice_destroy);
      require(prepared != nullptr, "Native voice codec unavailable");
      stop_voice();
      voice = prepared.release();
      voice_key = key;
      voice_codec = codec;
      voice_input = id;
      {
        std::lock_guard<std::mutex> lock(bridge_mutex);
        api.bh_up_clear(uplink, -1);
      }
      voice_handle = handle;
      voice_stop = false;
      voice_rx_packets = voice_tx_packets = voice_plc = voice_decode_errors = voice_rx_bytes =
          voice_tx_bytes = voice_frames = 0;
      api.bh_mix_call(mixer, id);
      api.bh_mix_clear(mixer, id);
      api.bh_mix_gate(mixer, id, true);
      voice_worker = std::thread([this] { voice_loop(); });
      return {{"epoch", voice_epoch.load()}};
    }
    if (op == "voice_stop") {
      if (request.value("epoch", voice_epoch.load()) != voice_epoch ||
          request.value("key", voice_key) != voice_key)
        return true;
      stop_voice();
      schedule("input", "", false);
      schedule("system", "", false);
      return true;
    }
    if (op == "audio_switch") {
      require(!quiesced || !request.value("start", true), "Receiver is stopping");
      auto direction = request.at("direction").get<std::string>();
      require(direction == "input" || direction == "output" || direction == "system",
              "Invalid audio direction");
      auto generation = request.value("voice_epoch", 0u);
      if (generation &&
          (generation != voice_epoch || (request.value("start", true) && voice_handle == 0xffff)))
        return 0;
      return schedule(request.at("direction"), request.value("endpoint_id", std::string()),
                      request.value("start", true), generation);
    }
    if (op == "audio_cancel") {
      std::lock_guard<std::mutex> lock(audio_mutex);
      uint64_t id = request.at("ticket");
      if (audio_results.count(id))
        return false;
      audio_results[id] = {{"done", true}, {"success", false}, {"cancelled", true}};
      for (auto &latest : latest_job)
        if (latest == id)
          latest = ++next_job;
      return true;
    }
    if (op == "audio_result") {
      std::lock_guard<std::mutex> lock(audio_mutex);
      uint64_t id = request.at("ticket");
      if (!id)
        return {{"done", true}, {"success", true}, {"cancelled", true}};
      auto found = audio_results.find(id);
      return found == audio_results.end() ? json{{"done", false}} : found->second;
    }
    if (op == "system_audio") {
      require(!quiesced || !request.value("enabled", false), "Receiver is stopping");
      auto generation = request.value("voice_epoch", 0u);
      if (generation && generation != voice_epoch)
        return 0;
      mix_enabled = request.at("enabled").get<bool>();
      return schedule("system", "", mix_enabled, generation);
    }
    if (op == "snapshot")
      return snapshot();
    throw std::runtime_error("Unsupported native operation");
  }
#include "runtime_replay.hpp"
  json snapshot() {
    json sources = json::object();
    for (const auto &pair : keys) {
      int id = pair.second;
      sources[pair.first] = {{"queued_frames", api.bh_mix_value(mixer, id, 0)},
                             {"dropped_frames", api.bh_mix_value(mixer, id, 1)},
                             {"underruns", api.bh_mix_value(mixer, id, 3)},
                             {"prefill_frames", api.bh_mix_value(mixer, id, 4)},
                             {"adaptive_target_ms", api.bh_mix_value(mixer, id, 5) / 48.0},
                             {"jitter_ms", api.bh_mix_value(mixer, id, 6) / 48.0},
                             {"concealed_frames", api.bh_mix_value(mixer, id, 7)},
                             {"concealed_silent_frames", api.bh_mix_value(mixer, id, 8)},
                             {"merged_frames", api.bh_mix_value(mixer, id, 9)},
                             {"rebuffer_events", api.bh_mix_value(mixer, id, 10)},
                             {"expanded_frames", api.bh_mix_value(mixer, id, 11)},
                             {"accelerated_frames", api.bh_mix_value(mixer, id, 12)}};
    }
    json up = json::object();
    for (int bus = 0; bus < 2; bus++)
      up[bus ? "system" : "microphone"] = {
          {"queued_frames", api.bh_up_value(uplink, bus, 0)},
          {"dropped_frames", api.bh_up_value(uplink, bus, 1)},
          {"drift_ppm", static_cast<int64_t>(api.bh_up_value(uplink, bus, 2)) - 1000},
          {"underruns", api.bh_up_value(uplink, bus, 3)},
          {"prefill_frames", api.bh_up_value(uplink, bus, 4)}};
    return {{"version", version},
            {"native_runtime", true},
            {"gains",
             {{"playback", playback_gain.load()},
              {"effective_playback", api.bh_mix_master_value(mixer)},
              {"microphone", microphone_gain.load()},
              {"system", system_gain.load()},
              {"microphone_muted", microphone_muted.load()}}},
            {"microphone_active", microphone_active.load()},
            {"system_active", system_active.load()},
            {"output_active", output_active.load()},
            {"sources", sources},
            {"usb", usb ? usb->snapshot() : json{{"active", false}}},
            {"microphone_frames", bridge_frames.load()},
            {"system_frames", system_frames.load()},
            {"bridge_errors", bridge_errors.load()},
            {"device_reopens", device_reopens.load()},
            {"uplink", up},
            {"voice",
             {{"rx_packets", voice_rx_packets.load()},
              {"tx_packets", voice_tx_packets.load()},
              {"rx_bytes", voice_rx_bytes.load()},
              {"tx_bytes", voice_tx_bytes.load()},
              {"decoded_frames", voice_frames.load()},
              {"plc_frames", voice_plc.load()},
              {"decode_errors", voice_decode_errors.load()},
              {"active", voice_handle != 0xffff}}}};
  }
};
} // namespace bh
