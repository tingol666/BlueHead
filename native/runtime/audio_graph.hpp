/* Native audio graph and registered HCI fast paths. Apache-2.0. */
#pragma once
#include "runtime_api.hpp"
#include "runtime_resource.hpp"
#include "runtime_codec_worker.hpp"
#include "hci_dispatch.hpp"
#include "iso_sender.hpp"
#include "runtime_hci_events.hpp"
#include "audio/media_worker.hpp"
#include "audio/iso_receiver.hpp"
#include "audio/iso_transmitter.hpp"
#include "audio/endpoint_actor.hpp"
#include "runtime_stream_guard.hpp"
#include "audio_timeline.h"
#include "link_identity.hpp"
#include "hci_links.hpp"
#include "runtime_recovery.hpp"
#include <deque>
#include <future>
#include <fstream>
#include <tuple>
namespace bh {
class AudioGraph final : public HciTarget {
public:
  Api api; // First member: DLLs outlive every resource, including constructor failure.
private:
  LinkIdentity link_identities;
  NativeResource mixer_owner, uplink_owner;
  bool offline = false, enhance_audio = true;
  std::atomic<bool> quiesced{false};
  std::mutex parameter_mutex;
  std::atomic<bool> replay_stop{false}, replaying{false};
  std::atomic<uint64_t> replay_started{0};
  std::thread replay_worker;
  std::mutex replay_mutex;
  json replay_result = json::object();
  std::unique_ptr<HciDispatch> test_dispatch;
  std::unique_ptr<HciLinks> test_link_lifetime;
  std::atomic<uint32_t> voice_stall{0}, voice_commit_stall{0};
  std::atomic<uint64_t> injected_stalls{0};
  void test_pause(std::atomic<uint32_t> &request) {
    auto ms = request.exchange(0);
    if (!ms)
      return;
    injected_stalls++;
    auto until = ticks() + frequency() * ms / 1000;
    while (!quitting && ticks() < until)
      Sleep(1);
  }
  struct Wire {
    uint32_t size = 0, epoch = 0;
    std::array<uint8_t, 120> bytes{};
  };
  std::unique_ptr<MediaServices> media_services;
  std::array<std::unique_ptr<MediaStream>, 8> media;
  std::array<std::unique_ptr<IsoReceiver>, 8> iso_receivers;
  std::unique_ptr<IsoTransmitter> iso_transmitter;
  std::string iso_tx_key;
  int iso_tx_input = -1;
  std::atomic<uint32_t> capture_owner{0xffff};
  std::map<std::string, int> keys;
  std::atomic<bool> transport_available{false};
  void *mixer = nullptr, *uplink = nullptr;
  std::atomic<bool> quitting{false}, voice_stop{true}, voice_rx_enabled{false};
  std::atomic<uint32_t> voice_handle{0xffff}, voice_codec{0}, voice_epoch{1}, voice_producers{0},
      voice_consumers{0};
  std::atomic<int> voice_input{-1};
  Spsc<SmallPacket, 128> voice_rx;
  Spsc<Wire, 16> voice_tx;
  Wire tx_partial;
  uint32_t tx_offset = 0, tx_seen_epoch = 0;
  std::thread voice_worker;
  Handle voice_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  bh_voice *voice = nullptr;
  std::atomic<uint64_t> voice_rx_packets{0}, voice_tx_packets{0}, voice_plc{0},
      voice_decode_errors{0}, voice_rx_bytes{0}, voice_tx_bytes{0}, voice_frames{0};
  std::atomic<uint64_t> voice_expired_packets{0}, voice_rx_resets{0};
  std::atomic<float> microphone_gain{1}, system_gain{1}, device_gain{1};
  std::atomic<float> playback_gain{1};
  std::array<uint64_t, 3> master_revisions{};
  uint64_t mute_revision = 0;
  std::atomic<bool> microphone_muted{false}, mix_enabled{false}, single{false};
  std::atomic<int> media_owner{-1};
  std::string voice_key;
  std::unique_ptr<EndpointActor> endpoints;
  uint32_t media_buffer = 2880, voice_buffer = 1440;
  fs::path assets;
  int key_input(const std::string &key) {
    auto found = keys.find(key);
    if (found == keys.end())
      throw SourceExpired();
    return found->second;
  }
  void voice_loop() {
    Priority priority;
    SmallPacket p;
    Wire wire;
    std::array<float, 11520> pcm{};
    Timer timer;
    uint32_t consecutive_missing = 0;
    uint64_t expected = 0;
    uint64_t source_position = 0;
    uint32_t pcm_rate = api.bh_voice_pcm_rate(voice);
    uint32_t frame_samples = pcm_rate * 75 / 10000;
    bool rx_recovery = false;
    double rx_retry_at = 0;
    uint64_t frame_ticks = frequency() * 75 / 10000;
    while (!voice_stop) {
      test_pause(voice_stall);
      if (voice_handle == 0xffff || !voice_rx_enabled)
        expected = 0;
      unsigned batch = 0;
      while (batch++ < 64 && voice_rx.pop(p)) {
        if (p.epoch != voice_epoch || voice_handle == 0xffff)
          continue;
        if (ticks() > p.arrival + frequency() * 90 / 1000) {
          voice_expired_packets++;
          api.bh_voice_skip_rx(voice, p.size);
          rx_recovery = true;
          expected = 0;
          continue;
        }
        if (rx_recovery) {
          if (seconds() < rx_retry_at)
            continue;
          if (api.bh_voice_reset_rx(voice) < 0) {
            voice_decode_errors++;
            rx_retry_at = seconds() + 1;
            continue;
          }
          rx_recovery = false;
          voice_rx_resets++;
          consecutive_missing = 0;
          source_position = 0;
          if (voice_input >= 0)
            api.bh_mix_clear(mixer, voice_input);
        }
        int frames = api.bh_voice_push_rx(voice, p.data.data(), p.size, p.status);
        if (frames < 0) {
          voice_decode_errors++;
          rx_recovery = true;
          rx_retry_at = seconds() + .1;
          expected = 0;
          continue;
        }
        if (frames > 0) {
          if (!expected || p.arrival > expected + frame_ticks * 16)
            expected = p.arrival;
          expected = std::max(expected + frame_ticks * frames, p.arrival + frame_ticks);
          consecutive_missing = 0;
        }
        uint32_t n = api.bh_voice_read_f32(voice, pcm.data(), pcm.size());
        test_pause(voice_commit_stall);
        if (p.epoch != voice_epoch || voice_handle == 0xffff)
          continue;
        if (age_ticks(p.arrival) > frequency() * 90 / 1000) {
          voice_expired_packets++;
          rx_recovery = true;
          expected = 0;
          continue;
        }
        if (n && voice_input >= 0) {
          source_position += n;
          api.bh_mix_observe(mixer, voice_input, bht_scale(source_position, pcm_rate, BHT_RATE),
                             p.arrival);
          api.bh_mix_write_f32_mono(mixer, voice_input, pcm.data(), n);
          voice_frames += bht_scale(n, pcm_rate, BHT_RATE);
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
          uint32_t n = api.bh_voice_read_f32(voice, pcm.data(), pcm.size());
          source_position += n;
          if (n && voice_input >= 0)
            api.bh_mix_write_f32_mono(mixer, voice_input, pcm.data(), n);
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
        if (api.bh_up_read_f32(uplink, pcm.data(), frame_samples) >= 0) {
          wire.epoch = voice_epoch;
          int n = api.bh_voice_encode_f32(voice, pcm.data(), frame_samples, wire.bytes.data(),
                                          wire.bytes.size());
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
  void stop_iso_tx() {
    if (!iso_transmitter || !iso_transmitter->prepared()) return;
    capture_owner=0xffff; iso_transmitter->retire(); voice_epoch++;
    iso_tx_input=-1; iso_tx_key.clear();
    api.bh_mix_call(mixer,-1);
  }
  void stop_voice() {
    auto old_handle=voice_handle.exchange(0xffff);
    if (capture_owner==old_handle) capture_owner=0xffff;
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
    int retired_input = voice_input.exchange(-1);
    if (retired_input >= 0)
      api.bh_mix_clear(mixer, retired_input);
  }

public:
  explicit AudioGraph(const fs::path &path, bool offline_test = false)
      : api(path), offline(offline_test), assets(path) {
    api.bh_media_log_quiet();
    mixer_owner.reset(api.bh_mix_create(), api.bh_mix_free);
    uplink_owner.reset(api.bh_up_create(), api.bh_up_free);
    mixer = mixer_owner.get();
    uplink = uplink_owner.get();
    require(mixer && uplink, "Native audio graph allocation failed");
    api.bh_up_rate(uplink, 48000);
    api.bh_up_adaptive(uplink, 1);
    api.bh_up_continuity(uplink, 1);
    media_services = std::make_unique<MediaServices>(MediaServices{
        api, assets, mixer, enhance_audio, quitting, injected_stalls,
        [this](int input, uint64_t now) {
          for (const auto &other : media)
            if (other && other->input != input && other->allowed && other->handle != 0xffff &&
                !other->paused && elapsed_ticks(now, other->last_packet.load()) < frequency())
              return true;
          return false;
        }});
    for (int i = 0; i < 8; i++) {
      media[i] = std::make_unique<MediaStream>(*media_services, i);
      iso_receivers[i] = std::make_unique<IsoReceiver>(*media_services, i);
    }
    iso_transmitter=std::make_unique<IsoTransmitter>(api,uplink,microphone_gain,system_gain,device_gain,microphone_muted);
    endpoints =
        std::make_unique<EndpointActor>(api, assets, mixer, uplink, offline, quiesced, voice_epoch,
                                        capture_owner, mix_enabled, voice_wake, injected_stalls);
  }
  ~AudioGraph() {
    replay_stop = true;
    if (replay_worker.joinable())
      replay_worker.join();
    stop_iso_tx();
    stop_voice();
    for (auto &iso : iso_receivers) iso->retire();
    for (auto &m : media)
      m->retire();
    quitting = true;
    endpoints.reset();
    for (auto &m : media)
      m.reset();
  }
  bool iso_handle(uint16_t handle) const override {
    for (const auto &iso : iso_receivers) if (iso->owns(handle)) return true;
    return false;
  }
  bool iso_packet(const uint8_t *p, uint32_t n) override {
    if (!p || n < 4) return false;
    for (auto &iso : iso_receivers)
      if (iso->owns(le16(p) & 0xfff)) return iso->push(p, n);
    return false;
  }
  bool iso_transmit_route(uint16_t &handle,uint32_t &epoch) const override {
    return !quiesced && iso_transmitter->route(handle,epoch);
  }
  uint32_t iso_output(uint32_t limit,uint8_t *out,uint32_t capacity,uint32_t &epoch) override {
    return quiesced?0:iso_transmitter->output(limit,out,capacity,epoch);
  }
  void iso_clock_sample(uint16_t handle,uint32_t epoch,uint16_t sequence,uint32_t stamp,uint32_t offset,uint64_t host) override {
    iso_transmitter->controller_clock(handle,epoch,sequence,stamp,offset,host);
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
  void connected(uint16_t h) override { link_identities.open(h); }
  uint64_t link_identity(uint16_t h) const override { return link_identities.get(h); }
  uint64_t last_link_identity() const override { return link_identities.last(); }
  void disconnected(uint16_t handle) override {
    link_identities.close(handle);
    uint16_t old_iso; uint32_t generation;
    bool had_iso=iso_transmitter->route(old_iso,generation);
    iso_transmitter->disconnect(handle);
    if (had_iso && !iso_transmitter->route(old_iso,generation)) capture_owner=0xffff;
    for (auto &iso : iso_receivers) iso->disconnect(handle);
    for (auto &m : media)
      if (m->handle == handle) {
        m->paused = true;
        m->epoch++;
        m->handle = 0xffff;
        api.bh_mix_connected(mixer,m->input,false);
      }
    if (voice_handle == handle) {
      capture_owner=0xffff;
      voice_handle = 0xffff;
      voice_rx_enabled = false;
      api.bh_mix_call(mixer, -1);
      if (voice_input >= 0)
        api.bh_mix_connected(mixer, voice_input, false);
      SetEvent(voice_wake);
    }
  }
  void transport_lost() noexcept override {
    transport_available = false;
    link_identities.clear();
    capture_owner=0xffff; iso_transmitter->transport_lost();
    for (auto &iso : iso_receivers) iso->transport_lost();
    for (auto &m : media) {
      m->paused = true;
      m->epoch++;
      m->handle = 0xffff;
      api.bh_mix_connected(mixer, m->input, false);
    }
    voice_handle = 0xffff;
    voice_rx_enabled = false;
    api.bh_mix_call(mixer, -1);
    SetEvent(voice_wake);
  }
  void transport_opened() {
    std::lock_guard<std::mutex> lock(parameter_mutex);
    transport_available = true;
    quiesced = false;
    api.bh_mix_master(mixer, playback_gain);
  }
  void quiesce_now() {
    std::lock_guard<std::mutex> lock(parameter_mutex);
    quiesced = true;
    capture_owner=0xffff;
    if (iso_transmitter) iso_transmitter->transport_lost();
    api.bh_mix_master(mixer, 0);
  }
  json command(const json &request) {
    std::string op = request.at("op").get<std::string>();
    if (auto result = endpoints->command(request))
      return *result;
    if (offline && (op == "audio_switch" || op == "system_audio"))
      throw std::runtime_error("Hardware opening is disabled in offline-test mode");
    if (request.contains("lease")) {
      auto id = key_input(request.at("key"));
      if (request.at("lease").get<uint32_t>() != media[id]->lease)
        throw SourceExpired();
    }
    if (op == "test_le_replay") {
      start_iso_replay(request); return true;
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
            age_ticks(replay_started.load()) / static_cast<double>(frequency());
      return result;
    }
    if (op == "test_replay_stop") {
      require(offline, "Offline-test mode required");
      replay_stop = true;
      if (replay_worker.joinable())
        replay_worker.join();
      return true;
    }
    if(op=="test_codec_fault") {
      require(offline && !replaying,"Exclusive offline-test mode required");
      auto &slot=*media[key_input(request.at("key"))];
      auto mode=request.at("mode").get<uint32_t>();
      require((slot.codec==5 || slot.codec==6) && (mode==1 || mode==2),"Invalid isolated codec fault");
      slot.isolated_fault=mode;return true;
    }
    if (op == "test_stall") {
      require(offline, "Offline-test mode required");
      auto direction = request.at("direction").get<std::string>();
      auto ms = request.value("milliseconds", 0u);
      require(ms > 0 && ms <= 2000, "Invalid native stall duration");
      if (direction == "media" || direction == "media_commit") {
        auto &slot = *media[key_input(request.value("key", std::string("offline:media")))];
        (direction == "media" ? slot.stall : slot.commit_stall) = ms;
        SetEvent(slot.wake);
      } else if (direction == "le_rx") {
        iso_receivers[key_input(request.at("key"))]->inject_stall(ms);
      } else if (direction == "le_tx") {
        iso_transmitter->inject_stall(ms);
      } else if (direction == "capture") {
        endpoints->stall_capture(ms);
      } else if (direction == "voice") {
        voice_stall = ms;
        SetEvent(voice_wake);
      } else if (direction == "voice_commit") {
        voice_commit_stall = ms;
        SetEvent(voice_wake);
      } else
        throw std::runtime_error("Unknown native worker");
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
      auto rate = request.value("pcm_rate", 48000u);
      require(rate >= 8000 && rate <= 384000, "Invalid guard PCM rate");
      for (const auto &event : events)
        states.push_back(guard.observe(event.at("time"), event.at("frames"), event.at("packets"),
                                       event.at("lost"), event.at("errors"), event.at("competing"),
                                       rate));
      return states;
    }

    if (op == "test_recovery_policy") {
      require(offline, "Offline-test mode required");
      auto events = request.at("events");
      require(events.is_array() && events.size() <= 512, "Invalid recovery input");
      Recovery state;
      DecoderRecovery decoder;
      json result = json::array();
      double previous = 0;
      for (const auto &event : events) {
        double now = event.at("time");
        require(std::isfinite(now) && now >= previous, "Recovery time must be monotonic");
        previous = now;
        auto action = event.value("action", std::string("fail"));
        if (action == "fail") {
          int code = event.at("code");
          state.fail(endpoints->classify_fault(code), code, now, event.value("automatic", true));
        } else if (action == "success")
          state.success(now);
        else if (action == "manual")
          state.manual();
        else if (action == "tick")
          state.healthy(now, event.value("active", true));
        else if (action != "decode")
          throw std::runtime_error("Invalid recovery action");
        auto value = state.snapshot(now);
        value["ready"] = state.ready(now);
        if (action == "decode")
          value["rebuild"] =
              decoder.observe(now, event.value("failed", false), event.value("malformed", false),
                              event.value("frames", uint64_t(0)));
        result.push_back(std::move(value));
      }
      return result;
    }
    if (op == "test_elapsed") {
      require(offline, "Offline-test mode required");
      return elapsed_ticks(request.at("now").get<uint64_t>(), request.at("stamp").get<uint64_t>());
    }

    if (op == "test_iso_clock") {
      require(offline,"Clock injection is disabled outside offline tests");
      iso_clock_sample(request.at("handle"),request.at("epoch"),request.at("sequence"),
                       request.at("timestamp"),request.value("offset",0u),request.value("host",ticks()));
      return true;
    }
    if (op == "test_iso_rx") {
      require(offline && !replaying, "Exclusive offline-test mode required");
      auto data = unhex(request.at("packet"), 4096);
      return iso_packet(data.data(), static_cast<uint32_t>(data.size()));
    }
    if (op == "test_iso_tx") {
      require(offline && !replaying,"Exclusive offline-test mode required");
      std::array<uint8_t,4104> data{}; uint32_t epoch=0;
      auto n=iso_output(request.value("payload_limit",251u),data.data(),data.size(),epoch);
      return {{"epoch",epoch},{"packet",hex(data.data(),n)}};
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
    if (op == "test_hci_event") {
      require(offline, "Offline-test mode required");
      auto data=unhex(request.at("packet"),260);
      if(!test_link_lifetime) {
        test_link_lifetime=std::make_unique<HciLinks>();
        test_link_lifetime->on_acquire(this,[](void *context,uint16_t h){static_cast<AudioGraph*>(context)->connected(h);});
      }
      auto valid=test_link_lifetime->event(data.data(),uint32_t(data.size()),[this](uint16_t h){disconnected(h);});
      return {{"valid",valid},{"epoch",packet_link_identity(*this,4,data.data(),uint32_t(data.size()))}};
    }
    if (op == "test_link_open") {
      require(offline, "Offline-test mode required");
      auto handle=request.at("handle").get<uint16_t>();
      require(handle<=0xeff,"Invalid link handle");connected(handle);return {{"epoch",link_identity(handle)}};
    }
    if (op == "test_disconnect") {
      require(offline, "Offline-test mode required");
      disconnected(request.at("handle"));
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
      json result = {{"frames", frames}, {"rms", std::sqrt(energy / (frames * 2))}, {"peak", peak}};
      if (request.value("return_pcm", false))
        result["pcm_f32"] = hex(reinterpret_cast<const uint8_t *>(pcm.data()), pcm.size() * sizeof(float));
      return result;
    }
    if (op == "hello")
      return {{"version", version}, {"abi", 1}, {"native_usb", true}, {"native_pcm_bridge", true}};
    if (op == "quiesce") {
      quiesce_now();
      stop_iso_tx();
      stop_voice();
      for (auto &iso : iso_receivers) iso->retire();
      for (auto &m : media) {
        m->paused = true;
        m->epoch++;
      }
      endpoints->schedule("input", "", false);
      endpoints->schedule("system", "", false);
      endpoints->schedule("output", "", false);
      return true;
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
      auto budget=request.value("buffer_us",uint32_t(0));
      require(!request.contains("buffer_us") || budget<=180000,"Invalid source delay budget");
      auto target=request.contains("buffer_us")?uint32_t(uint64_t(budget)*48/1000):(call?voice_buffer:media_buffer);
      auto maximum=call?4320u:8640u;
      require(target<=maximum,"Source delay exceeds ring bound");
      auto id = api.bh_mix_add(mixer, target, maximum);
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
      if(request.contains("buffer_us")) api.bh_mix_continuity(mixer,id,enhance_audio,target,target);
      api.bh_mix_timeline(mixer, id, enhance_audio ? (call ? BHT_VOICE : BHT_MEDIA) : BHT_GENERIC);
      return {{"index", id}, {"lease", media[id]->lease}};
    }
    if (op == "mix_remove") {
      auto key = request.at("key").get<std::string>();
      auto found = keys.find(key);
      if (found != keys.end()) {
        int id = found->second;
        if (id == iso_tx_input) {
          stop_iso_tx(); endpoints->schedule("input","",false); endpoints->schedule("system","",false);
        }
        if (id == voice_input) {
          stop_voice();
          endpoints->schedule("input", "", false);
          endpoints->schedule("system", "", false);
        }
        iso_receivers[id]->retire();
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
      if(!iso_transmitter->prepared()) device_gain = request.value("device", 1.0f);
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
    if (op == "le_tx_prepare" || op == "le_tx_bind" || op == "le_tx_stop" || op == "le_tx_snapshot" || op=="le_tx_gain") {
      require(request.contains("lease"),"LE source lease is required");
      auto key=request.at("key").get<std::string>();
      int id=key_input(key); auto epoch=request.at("epoch").get<uint32_t>();
      require(epoch!=0,"LE source epoch is required");
      if (op == "le_tx_prepare") {
        require(!quiesced && !voice && voice_handle==0xffff && capture_owner==0xffff,
                "Another voice source still owns the microphone");
        auto capture_budget=request.value("capture_buffer_us",30000u);
        require(capture_budget<=80000,"Invalid LC3 capture delay budget");
        iso_transmitter->prepare(request.at("rate"),request.at("duration_us"),request.at("octets"),request.value("blocks",1u),epoch,request.value("parent",0xffffu));
        if (api.bh_up_output_rate(uplink,request.at("rate"))!=0) {
          iso_transmitter->retire(); throw std::runtime_error("Cannot prepare LC3 microphone PCM rate");
        }
        if(api.bh_up_budget(uplink,uint32_t(uint64_t(capture_budget)*48/1000),request.contains("capture_buffer_us"))!=0) {
          iso_transmitter->retire();throw std::runtime_error("Cannot prepare LE capture delay budget");
        }
        iso_tx_key=key; iso_tx_input=id; endpoints->clear_uplink();
        return iso_transmitter->snapshot();
      }
      if (key!=iso_tx_key || !iso_transmitter->generation(epoch)) throw SourceExpired();
      if(op=="le_tx_gain") {
        auto gain=request.at("gain").get<float>();
        require(std::isfinite(gain) && gain>=0 && gain<=2,"Invalid LE source gain");
        device_gain=gain;
      } else if (op == "le_tx_bind") {
        auto handle=request.at("handle").get<uint32_t>(), parent=request.at("parent").get<uint32_t>();
        require(handle<=0xeff && parent<=0xeff,"Invalid LC3 microphone handle");
        iso_transmitter->bind(uint16_t(handle),uint16_t(parent),epoch);
        capture_owner=handle; voice_epoch++;
      } else if (op == "le_tx_stop") {
        stop_iso_tx(); endpoints->schedule("input","",false); endpoints->schedule("system","",false);
      }
      auto result=iso_transmitter->snapshot(); result["voice_epoch"]=voice_epoch.load();return result;
    }
    if (op == "le_rx_prepare" || op == "le_rx_bind" || op == "le_rx_stop" || op == "le_rx_snapshot") {
      require(request.contains("lease"), "LE stream lease is required");
      int id = key_input(request.at("key"));
      auto &iso = *iso_receivers[id];
      auto epoch = request.at("epoch").get<uint32_t>();
      require(epoch != 0, "LE stream epoch is required");
      if (op == "le_rx_prepare") {
        require(!quiesced, "Receiver is stopping");
        require(media[id]->handle == 0xffff, "A2DP route still owns this source");
        media[id]->retire();
        iso.configure(request.at("rate"), request.at("duration_us"), request.at("channels"),
                      request.at("octets"), request.value("blocks", 1u), epoch, request.value("voice", false), request.value("jitter_wait_us", 3000u),
                      request.value("split",false),request.value("broadcast",false),request.value("allocation",0u),
                      request.value("controller_timestamps",true),request.value("parent",0xffffu));
        api.bh_mix_timeline(mixer, id, request.value("voice", false) ? BHT_VOICE : BHT_MEDIA);
        return iso.snapshot();
      }
      if (!iso.generation(epoch)) throw SourceExpired();
      if (op == "le_rx_bind") {
        auto handle = request.at("handle").get<uint32_t>(), parent = request.at("parent").get<uint32_t>();
        require(handle<=0xeff && (parent<=0xeff || (parent==0xffff && iso.is_broadcast())) && handle!=parent,"Invalid ISO route");
        require(voice_handle != handle && !iso_handle(static_cast<uint16_t>(handle)), "ISO handle already registered");
        iso.bind(static_cast<uint16_t>(handle),static_cast<uint16_t>(parent),epoch,request.value("lane",0u));
        api.bh_mix_gate(mixer, id, media[id]->allowed);
      } else if (op == "le_rx_stop") {
        iso.retire(); api.bh_mix_gate(mixer, id, false); api.bh_mix_clear(mixer, id);
      }
      return iso.snapshot();
    }
    if (op == "media_config") {
      int id = key_input(request.at("key"));
      require(!iso_receivers[id]->prepared(), "LE stream still owns this mixer source");
      media[id]->configure(request.at("codec"), request.at("rate"), request.at("channels"),
                           request.at("key"), request.value("depth", 16u));
      return true;
    }
    if (op == "media_route") {
      auto id = key_input(request.at("key"));
      require(!iso_receivers[id]->prepared(), "LE stream still owns this mixer source");
      auto &m = *media[id];
      auto handle = request.at("handle").get<uint32_t>(), cid = request.at("cid").get<uint32_t>();
      require(handle <= 0xfff && cid >= 0x40 && cid <= 0xffff, "Invalid media route");
      m.handle = 0xffff;
      m.epoch++;
      m.cid = cid;
      m.handle = handle;
      api.bh_mix_connected(mixer,id,true);
      return true;
    }
    if (op == "media_unroute") {
      auto &m = *media[key_input(request.at("key"))];
      std::lock_guard<std::mutex> commit(m.commit_mutex);
      m.handle = m.cid = 0xffff;
      m.epoch++;
      m.paused = true;
      api.bh_mix_connected(mixer,m.input,false);
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
      require(offline || transport_available.load(), "Bluetooth transport is unavailable");
      auto key = request.at("key").get<std::string>();
      int id = key_input("call:" + key);
      require(!iso_transmitter->prepared(),"LE microphone source must stop before starting classic voice");
      uint32_t codec = request.at("codec");
      auto handle = request.at("handle").get<uint32_t>();
      require(handle <= 0xfff, "Invalid voice handle");
      uint32_t native_rate = codec == 3 ? 32000 : codec == 2 ? 16000 : 8000;
      std::unique_ptr<bh_voice, decltype(api.bh_voice_destroy)> prepared(
          api.bh_voice_create_at(codec, native_rate), api.bh_voice_destroy);
      require(prepared != nullptr, "Native voice codec unavailable");
      stop_voice();
      require(api.bh_mix_format(mixer, id, native_rate) == 0,
              "Cannot prepare native voice output format");
      require(api.bh_up_budget(uplink,1440,0)==0,"Cannot restore classic voice capture budget");
      require(api.bh_up_output_rate(uplink, native_rate) == 0,
              "Cannot prepare native voice input format");
      voice = prepared.release();
      voice_key = key;
      voice_codec = codec;
      voice_input = id;
      endpoints->clear_uplink();
      voice_handle = handle;
      capture_owner = handle;
      voice_stop = false;
      voice_rx_packets = voice_tx_packets = voice_plc = voice_decode_errors = voice_rx_bytes =
          voice_tx_bytes = voice_frames = 0;
      voice_expired_packets = voice_rx_resets = 0;
      api.bh_mix_call(mixer, id);
      api.bh_mix_clear(mixer, id);
      api.bh_mix_gate(mixer, id, true);
      api.bh_mix_connected(mixer,id,true);
      voice_worker = std::thread([this] { voice_loop(); });
      return {{"epoch", voice_epoch.load()}};
    }
    if (op == "voice_stop") {
      if (iso_transmitter->prepared() && !voice) return true;
      if (request.value("epoch", voice_epoch.load()) != voice_epoch ||
          request.value("key", voice_key) != voice_key)
        return true;
      stop_voice();
      endpoints->schedule("input", "", false);
      endpoints->schedule("system", "", false);
      return true;
    }

    if (op == "snapshot")
      return snapshot();
    throw std::runtime_error("Unsupported native operation");
  }
#include "../testing/runtime_replay.hpp"
#include "../testing/runtime_iso_replay.hpp"
  json snapshot() {
    json sources = json::object();
    for (const auto &pair : keys) {
      int id = pair.second;
      sources[pair.first] = {
          {"pcm_rate", api.bh_mix_value(mixer, id, 20)},
          {"queued_frames", api.bh_mix_value(mixer, id, 0)},
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
          {"accelerated_frames", api.bh_mix_value(mixer, id, 12)},
          {"expired_frames", api.bh_mix_value(mixer, id, 13)},
          {"gap_owner", api.bh_mix_value(mixer, id, 14)},
          {"written_frames", api.bh_mix_value(mixer, id, 15)},
          {"presented_frames", api.bh_mix_value(mixer, id, 16)},
          {"callback_period_searches", api.bh_mix_value(mixer, id, 17)},
          {"recovery_discarded_frames", api.bh_mix_value(mixer, id, 18)},
          {"invalid_samples", api.bh_mix_value(mixer, id, 19)},
          {"drift_ppm", static_cast<int64_t>(api.bh_mix_value(mixer, id, 2)) - 1000}};
      if (pair.first.rfind("call:", 0) != 0)
        sources[pair.first]["media"] = media[id]->snapshot();
      if (iso_receivers[id]->snapshot().value("epoch", 0u))
        sources[pair.first]["le_audio"] = iso_receivers[id]->snapshot();
    }

    json up = json::object();
    for (int bus = 0; bus < 2; bus++)
      up[bus ? "system" : "microphone"] = {
          {"pcm_rate", api.bh_up_value(uplink, bus, 20)},
          {"queued_frames", api.bh_up_value(uplink, bus, 0)},
          {"dropped_frames", api.bh_up_value(uplink, bus, 1)},
          {"drift_ppm", static_cast<int64_t>(api.bh_up_value(uplink, bus, 2)) - 1000},
          {"underruns", api.bh_up_value(uplink, bus, 3)},
          {"prefill_frames", api.bh_up_value(uplink, bus, 4)},
          {"gap_owner", api.bh_up_value(uplink, bus, 14)},
          {"expired_frames", api.bh_up_value(uplink, bus, 13)},
          {"callback_period_searches", api.bh_up_value(uplink, bus, 17)}};
    json result = {{"version", version},
                   {"native_runtime", true},
                   {"quality",
                    {{"limiter_lookahead_ms", api.bh_mix_value(mixer, -1, 1) / 48.0},
                     {"limited_frames", api.bh_mix_value(mixer, -1, 2)},
                     {"uplink_limiter_lookahead_ms", api.bh_up_value(uplink, -1, 1) / 48.0},
                     {"uplink_limited_frames", api.bh_up_value(uplink, -1, 2)}}},
                   {"gains",
                    {{"playback", playback_gain.load()},
                     {"effective_playback", api.bh_mix_master_value(mixer)},
                     {"microphone", microphone_gain.load()},
                     {"system", system_gain.load()},
                     {"microphone_muted", microphone_muted.load()}}},
                   {"sources", sources},
                   {"injected_native_stalls", injected_stalls.load()},
                   {"le_source", iso_transmitter->snapshot()},
                   {"uplink", up},
                   {"voice",
                    {{"rx_packets", voice_rx_packets.load()},
                     {"epoch", voice_epoch.load()},
                     {"tx_packets", voice_tx_packets.load()},
                     {"rx_bytes", voice_rx_bytes.load()},
                     {"tx_bytes", voice_tx_bytes.load()},
                     {"decoded_frames", voice_frames.load()},
                     {"plc_frames", voice_plc.load()},
                     {"decode_errors", voice_decode_errors.load()},
                     {"pcm_rate", voice ? api.bh_voice_pcm_rate(voice) : 0},
                     {"expired_packets", voice_expired_packets.load()},
                     {"rx_resets", voice_rx_resets.load()},
                     {"active", voice_handle != 0xffff}}}};
    result.update(endpoints->snapshot());
    return result;
  }
};
} // namespace bh
