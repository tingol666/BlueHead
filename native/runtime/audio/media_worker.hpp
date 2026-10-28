#pragma once
#include "runtime_api.hpp"
#include "runtime_codec_worker.hpp"
#include "runtime_stream_guard.hpp"
#include "runtime_recovery.hpp"
#include "audio_timeline.h"
#include <functional>
namespace bh {
/* Bounded services exposed to a media worker. No graph, USB or endpoint ownership. */
struct MediaServices {
  Api &api;
  const fs::path &assets;
  void *mixer;
  const bool &enhance_audio;
  const std::atomic<bool> &quitting;
  std::atomic<uint64_t> &injected_stalls;
  std::function<bool(int, uint64_t)> competing;
  void pause(std::atomic<uint32_t> &request) {
    auto ms = request.exchange(0);
    if (!ms)
      return;
    injected_stalls++;
    auto until = ticks() + frequency() * ms / 1000;
    while (!quitting && ticks() < until)
      Sleep(1);
  }
};
class MediaStream {
public:
  MediaServices &core;
  int input;
  BhMedia *decoder = nullptr;
  std::unique_ptr<IsolatedMediaWorker> isolated;
  std::unique_ptr<float[]> isolated_pcm;
  std::unique_ptr<uint8_t[]> isolated_fence;
  double isolated_retry = 0;
  std::array<uint64_t, 24> isolated_seen{};
  bool isolated_pending_reset = false;
  Spsc<Packet, 16> queue;
  Handle wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  std::thread worker;
  std::atomic<bool> stop{false}, paused{true}, restart{false};
  std::atomic<uint32_t> stall{0}, commit_stall{0}, isolated_fault{0};
  std::atomic<uint32_t> handle{0xffff}, cid{0xffff}, epoch{1}, producers{0};
  uint32_t lease = 0;
  std::atomic<uint64_t> packets{0}, drops{0}, decoded{0}, errors{0}, coded{0}, lost{0},
      recoveries{0}, last_packet{0}, last_decoded{0}, concealed{0}, timeline_resets{0};
  std::atomic<uint64_t> expired_packets{0}, decoder_restarts{0}, malformed_packets{0};
  std::atomic<uint64_t> producer_position_frames{0};
  std::atomic<uint64_t> gap_exact_source{0},gap_estimated_source{0},gap_omitted_unknown{0},
      gap_omitted_exact_source{0};
  DecoderRecovery failure;
  bool recovery_restart = false;
  bool decoder_repair = false;
  double decoder_repair_after = 0;
  double restart_after = 0;
  uint32_t codec = 0, rate = 0, channels = 0, depth = 16;
  std::string key;
  std::mutex commit_mutex;
  StreamGuard guard;
  std::atomic<bool> allowed{true}, protected_audio{false}, guard_reset{true};
  uint64_t meter_start = 0, meter_bytes = 0, meter_packets = 0;
  std::atomic<uint64_t> bitrate{0};
  std::atomic<double> packets_per_second{0};
  explicit MediaStream(MediaServices &c, int id) : core(c), input(id) {}
  void reset_observations() {
    packets = drops = decoded = errors = coded = lost = recoveries = last_packet = last_decoded =
        concealed = timeline_resets = expired_packets = decoder_restarts = malformed_packets = 0;
    meter_start = meter_bytes = meter_packets = 0;
    bitrate = 0;
    packets_per_second = 0;
    producer_position_frames = 0;
    gap_exact_source=gap_estimated_source=gap_omitted_unknown=gap_omitted_exact_source=0;
  }
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
    isolated.reset();
    isolated_pcm.reset();
    isolated_fence.reset();
    Packet discard;
    while (queue.pop(discard)) {
    };
    paused = true;
    codec = rate = channels = 0;
    reset_observations();
  }
  ~MediaStream() { retire(); }
  void configure(int code, int hz, int ch, const std::string &k, uint32_t bits = 16) {
    std::unique_ptr<BhMedia, decltype(core.api.bh_media_destroy)> prepared(
        nullptr, core.api.bh_media_destroy);
    std::unique_ptr<IsolatedMediaWorker> prepared_isolated;
    std::unique_ptr<float[]> prepared_pcm;
    std::unique_ptr<uint8_t[]> prepared_fence;
    if (code == 5 || code == 6) {
      prepared_isolated = std::make_unique<IsolatedMediaWorker>(core.assets, hz, ch, code, bits);
      prepared_pcm = std::make_unique<float[]>(48064 * 2);
      prepared_fence = std::make_unique<uint8_t[]>(48064);
    } else {
      prepared.reset(core.api.bh_media_create(code, hz, ch, hz));
      require(prepared != nullptr, "Native media codec unavailable");
      core.api.bh_media_continuity(prepared.get(), core.enhance_audio);
    }
    {
      std::lock_guard<std::mutex> commit(commit_mutex);
      bool was_paused = paused.exchange(true);
      if (core.api.bh_mix_format(core.mixer, input, hz) < 0) {
        paused = was_paused;
        throw std::runtime_error("Cannot prepare native media PCM format");
      }
    }
    retire();
    codec = code;
    failure = {};
    recovery_restart = false;
    decoder_repair = false;
    decoder_repair_after = 0;
    restart_after = 0;
    expired_packets = decoder_restarts = malformed_packets = 0;
    rate = hz;
    channels = ch; depth = bits;
    key = k;
    reset_observations();
    decoder = prepared.release();
    isolated = std::move(prepared_isolated);
    isolated_seen.fill(0);
    isolated_pending_reset = false;
    isolated_pcm = std::move(prepared_pcm);
    isolated_fence = std::move(prepared_fence);
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
    bool competing = allowed && core.competing(input, ticks());
    protected_audio = guard.observe(arrival / static_cast<double>(frequency()), frames, 1, missing,
                                    failed, competing, rate);
    core.api.bh_mix_gate(core.mixer, input, allowed && !protected_audio);
  }
  bool past_deadline(const Packet &p) {
    if (age_ticks(p.arrival) <= frequency() * 180 / 1000)
      return false;
    expired_packets++;
    recovery_restart = true;
    restart = true;
    return true;
  }
  void run() {
    Priority priority;
    Packet p;
    while (!stop) {
      WaitForSingleObject(wake, INFINITE);
      while (!stop && queue.pop(p)) {
        core.pause(stall);
        if (paused || p.epoch != epoch)
          continue;
        // A worker wake is not permission to play arbitrarily old sound.
        // Only this consumer retires compressed entries; the HCI producer
        // never mutates an SPSC read pointer or a decoder instance.
        if (past_deadline(p))
          continue;
        if (restart && seconds() < restart_after)
          continue;
        if (restart.exchange(false)) {
          decoder_repair = false; // Explicit/expired source retirement wins.
          decoder_repair_after = 0;
          producer_position_frames = 0;
          meter_start = meter_bytes = meter_packets = 0;
          bitrate = 0;
          packets_per_second = 0;
          if (decoder) {
            if (core.api.bh_media_restart(decoder) < 0) {
              errors++;
              restart = true;
              restart_after = seconds() + 1;
              continue;
            }
          } else
            isolated.reset();
          if (recovery_restart) {
            decoder_restarts++;
            recovery_restart = false;
          }
        } else if (decoder_repair && decoder) {
          if (seconds() < decoder_repair_after) continue;
          if (core.api.bh_media_recover_decoder(decoder) < 0) {
            errors++;
            decoder_repair_after = seconds() + 1;
            continue;
          }
          decoder_repair = false;
          decoder_repair_after = 0;
          decoder_restarts++;
        }
        if (codec == 5 || codec == 6) {
          try {
            if (!isolated) {
              if (seconds() < isolated_retry)
                continue;
              isolated = std::make_unique<IsolatedMediaWorker>(core.assets, rate, channels, codec, depth);
              isolated_seen.fill(0);
            }
            if(auto fault=isolated_fault.exchange(0)) isolated->inject_fault(fault);
            uint32_t free_frames=0, remaining_frames=0, uncertain=0;
            core.api.bh_mix_gap_budget(core.mixer,input,rate,p.arrival,&free_frames,&remaining_frames);
            uint64_t stats[24]{};
            uint32_t frames = isolated->feed(p.data.data(), p.size, isolated_pcm.get(),
                                             isolated_fence.get(),stats,free_frames,remaining_frames,uncertain);
            auto delta = [&](size_t field) {
              return stats[field] >= isolated_seen[field] ? stats[field] - isolated_seen[field]
                                                      : stats[field];
            };
            uint64_t genuine = delta(0), newly_lost = delta(5), failed = delta(3);
            coded += delta(2);
            lost += newly_lost;
            recoveries += delta(4);
            errors += failed;
            malformed_packets += delta(9);
            concealed += delta(10);
            timeline_resets += delta(11);
            gap_exact_source+=delta(20); gap_estimated_source+=delta(21);
            gap_omitted_unknown+=delta(22); gap_omitted_exact_source+=delta(23);
            isolated_pending_reset = isolated_pending_reset || delta(11) != 0;
            std::copy(std::begin(stats), std::end(stats), isolated_seen.begin());
            core.pause(commit_stall);
            std::lock_guard<std::mutex> commit(commit_mutex);
            if (!paused && p.epoch == epoch && !past_deadline(p)) {
              update_guard(p.arrival, genuine, newly_lost, failed);
              if (isolated_pending_reset) {
                core.api.bh_mix_clear(core.mixer, input);
                isolated_pending_reset = false;
              }
              if (frames) {
                // The decoded END position is trustworthy only across known
                // intervals. Estimates/omissions carry their explicit fence
                // and reanchor arrival confidence; they never train jitter as
                // if their synthetic duration were an exact sample clock.
                auto position = bht_scale(stats[0] + stats[10], rate, BHT_RATE);
                producer_position_frames = position;
                core.api.bh_mix_observe_confident(core.mixer,input,position,p.arrival,uncertain);
                core.api.bh_mix_write_fenced(core.mixer,input,isolated_pcm.get(),isolated_fence.get(),frames);
                decoded += genuine;
                if (genuine)
                  last_decoded = ticks();
              }
            }
            update_meter(p.arrival);
          } catch (const std::exception &) {
            errors++;
            isolated.reset();
            isolated_retry = seconds() + 1;
          }
          continue;
        }
        auto before_frames = core.api.bh_media_value(decoder, 0),
             before_errors = core.api.bh_media_value(decoder, 3),
             before_lost = core.api.bh_media_value(decoder, 5),
             before_malformed = core.api.bh_media_value(decoder, 9);
        uint32_t free_frames=0, remaining_frames=0;
        core.api.bh_mix_gap_budget(core.mixer,input,rate,p.arrival,&free_frames,&remaining_frames);
        core.api.bh_media_gap_context(decoder,free_frames,remaining_frames);
        int result = core.api.bh_media_feed(decoder, p.data.data(), p.size);
        core.pause(commit_stall);
        auto real_frames = core.api.bh_media_value(decoder, 0) - before_frames;
        auto failed = core.api.bh_media_value(decoder, 3) - before_errors;
        if (result < 0 && !failed)
          failed = 1;
        errors += failed;
        auto malformed = core.api.bh_media_value(decoder, 9) - before_malformed;
        malformed_packets += malformed;
        if (failure.observe(seconds(), failed != 0, malformed != 0, real_frames)) {
          decoder_repair = true;
        }
        std::lock_guard<std::mutex> commit(commit_mutex);
        if (!paused && p.epoch == epoch && !past_deadline(p)) {
          update_guard(p.arrival, real_frames, core.api.bh_media_value(decoder, 5) - before_lost,
                       failed);
          core.api.bh_media_drain_mixer_at(decoder, core.mixer, input, p.arrival);
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
        gap_exact_source=core.api.bh_media_value(decoder,20);
        gap_estimated_source=core.api.bh_media_value(decoder,21);
        gap_omitted_unknown=core.api.bh_media_value(decoder,22);
        gap_omitted_exact_source=core.api.bh_media_value(decoder,23);
      }
    }
  }
  void update_meter(uint64_t now) {
    auto bytes = coded.load();
    if (!meter_start || bytes < meter_bytes) {
      meter_start = now;
      meter_bytes = bytes;
      meter_packets = packets.load();
      return;
    }
    if (now - meter_start >= frequency() / 2) {
      bitrate = (bytes - meter_bytes) * 8 * frequency() / (now - meter_start);
      auto count = packets.load();
      packets_per_second = (count - meter_packets) * static_cast<double>(frequency()) / (now - meter_start);
      meter_start = now;
      meter_bytes = bytes;
      meter_packets = count;
    }
  }
  json snapshot() {
    auto last = last_packet.load(), recent = last_decoded.load();
    auto packet_age = age_ticks(last), decoded_age = age_ticks(recent);
    return {{"codec", codec},
            {"rate", rate},
            {"packets", packets.load()},
            {"dropped_packets", drops.load()},
            {"decoded_frames", decoded.load()},
            {"decode_errors", errors.load()},
            {"coded_bytes", coded.load()},
            {"bitrate", !paused && last && packet_age < frequency() * 3 / 2 ? bitrate.load() : 0},
            {"packets_per_second", !paused && last && packet_age < frequency() * 3 / 2 ? packets_per_second.load() : 0},
            {"lost_packets", lost.load()},
            {"concealed_frames", concealed.load()},
            {"timeline_resets", timeline_resets.load()},
            {"expired_packets", expired_packets.load()},
            {"decoder_restarts", decoder_restarts.load()},
            {"malformed_packets", malformed_packets.load()},
            {"producer_position_frames", producer_position_frames.load()},
            {"gap_source_rate",rate},
            {"gap_exact_source_frames",gap_exact_source.load()},
            {"gap_estimated_source_frames",gap_estimated_source.load()},
            {"gap_omitted_unknown_units",gap_omitted_unknown.load()},
            {"gap_omitted_exact_source_frames",gap_omitted_exact_source.load()},
            {"recoveries", recoveries.load()},
            {"protected", protected_audio.load()},
            {"playing", !paused && recent && decoded_age < frequency() * 3 / 2},
            {"buffer_ms", core.api.bh_mix_value(core.mixer, input, 0) / 48.0},
            {"last_packet_age_ms", last ? packet_age * 1000.0 / frequency() : 1e12}};
  }
};
} // namespace bh
