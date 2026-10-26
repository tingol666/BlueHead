/* Endpoint lifecycle and PCM producer bridge. No USB, media source or voice codec ownership. */
#pragma once
#include "runtime_api.hpp"
#include "runtime_recovery.hpp"
#include <deque>
#include <map>
#include <optional>
#include <tuple>
namespace bh {
class EndpointActor {
  Api &api;
  const fs::path assets;
  void *mixer, *uplink;
  const bool offline;
  const std::atomic<bool> &quiesced;
  const std::atomic<uint32_t> &voice_epoch, &voice_handle;
  std::atomic<bool> &mix_enabled;
  const HANDLE voice_wake;
  std::atomic<uint64_t> &injected_stalls;
  std::atomic<bool> quitting{false};
  std::atomic<uint32_t> bridge_stall{0};
  std::thread bridge_worker, audio_worker, watchdog_worker;
  std::atomic<bool> watchdog_stop{false};
  std::atomic<uint64_t> audio_deadline{0};
  Handle watchdog_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  Handle bridge_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  void test_pause(std::atomic<uint32_t> &request) {
    auto ms = request.exchange(0);
    if (!ms)
      return;
    injected_stalls++;
    auto until = ticks() + frequency() * ms / 1000;
    while (!quitting && ticks() < until)
      Sleep(1);
  }
  struct AudioJob {
    uint64_t id = 0;
    std::string direction, endpoint;
    bool start = true;
    uint32_t voice_generation = 0;
    bool simulated = false;
    uint32_t delay_ms = 0;
    bool automatic = false;
    int simulated_error = 0;
    bool force = false;
  };
  std::atomic<uint64_t> bridge_frames{0}, system_frames{0}, bridge_errors{0};
  std::mutex bridge_mutex;
  BhDeviceContext *context = nullptr;
  BhAudioDevice *output = nullptr, *capture = nullptr;
  void *loop_capture = nullptr, *loop_converter = nullptr;
  std::string output_selection, input_selection;
  std::array<std::string, 2> wanted_endpoint{};
  std::array<std::string, 2> retry_endpoint{};
  std::atomic<bool> simulated_inventory_enabled{false};
  std::vector<BhEndpoint> simulated_endpoints;
  std::array<int, 2> simulated_prepare_errors{};
  bool output_wanted = false, capture_wanted = false, system_wanted = false;
  std::array<Recovery, 3> recovery{};
  std::atomic<bool> microphone_fault{false}, system_fault{false};
  std::mutex audio_mutex;
  std::condition_variable audio_condition;
  std::deque<AudioJob> audio_jobs;
  std::map<uint64_t, json> audio_results;
  uint64_t next_job = 0;
  std::array<uint64_t, 3> latest_job{};
  std::array<bool, 3> preparing_audio{};
  std::atomic<uint64_t> device_reopens{0};
  std::atomic<bool> microphone_active{false}, system_active{false}, output_active{false};
  json device_error = json::object();
  std::mutex diagnostic_mutex;
  json device_diagnostics = json::object();

public:
  static int direction_id(const std::string &s) { return s == "output" ? 0 : s == "input" ? 1 : 2; }
  uint64_t schedule(const std::string &direction, const std::string &endpoint, bool start = true,
                    uint32_t generation = 0, bool simulated = false, uint32_t delay_ms = 0,
                    uint64_t expected_latest = UINT64_MAX, bool automatic = false,
                    int simulated_error = 0, bool force = false) {
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
    audio_jobs.push_back({id, direction, endpoint, start, generation, simulated, delay_ms,
                          automatic, simulated_error, force});
    audio_condition.notify_one();
    return id;
  }

private:
  void finish(const AudioJob &j, const json &result) {
    std::lock_guard<std::mutex> lock(audio_mutex);
    audio_results[j.id] = result;
    while (audio_results.size() > 64)
      audio_results.erase(audio_results.begin());
  }
  Fault device_fault(int code) {
    switch (api.bh_device_error_category(code)) {
    case BH_FAILURE_ENDPOINT:
      return Fault::endpoint;
    case BH_FAILURE_BUSY:
      return Fault::busy;
    case BH_FAILURE_PERMISSION:
      return Fault::permission;
    case BH_FAILURE_FORMAT:
      return Fault::format;
    case BH_FAILURE_MEMORY:
      return Fault::memory;
    case BH_FAILURE_TIMEOUT:
      return Fault::timeout;
    default:
      return Fault::internal;
    }
  }
  void audio_failed(const AudioJob &job, Fault fault, int code, const char *message) {
    {
      std::lock_guard<std::mutex> lock(audio_mutex);
      if (latest_job[direction_id(job.direction)] != job.id) {
        audio_results[job.id] = {{"done", true}, {"success", false}, {"cancelled", true}};
        return;
      }
    }
    auto &state = recovery[direction_id(job.direction)];
    state.fail(fault, code, seconds(), job.automatic && job.start);
    bool previous_active =
        job.direction == "output" ? output && api.bh_device_active(output)
        : job.direction == "input"
            ? capture && api.bh_device_active(capture)
            : loop_capture && SUCCEEDED(static_cast<HRESULT>(api.bh_loop_error(loop_capture)));
    if (previous_active && !job.automatic)
      state.keep_previous(seconds());
    finish(job, {{"done", true},
                 {"success", false},
                 {"error", message},
                 {"category", fault_name(fault)},
                 {"native_code", code},
                 {"previous_route_active", previous_active},
                 {"retryable", job.automatic && state.automatic},
                 {"direction", job.direction}});
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
          preparing_audio[direction_id(job.direction)] = true;
          have = true;
        }
      }
      if (have) {
        audio_deadline = ticks() + frequency() * 12;
        try {
          execute_audio(job);
        } catch (const AudioFailure &e) {
          audio_failed(job, e.category, e.code, e.what());
        } catch (const std::exception &e) {
          audio_failed(job, Fault::internal, 0, e.what());
        }
        cache_device_diagnostics();
        {
          std::lock_guard<std::mutex> lock(audio_mutex);
          preparing_audio[direction_id(job.direction)] = false;
        }
        audio_deadline = 0;
      }
      if (seconds() >= next_check) {
        next_check = seconds() + .5;
        audio_deadline = ticks() + frequency() * 12;
        try {
          check_devices();
          cache_device_diagnostics();
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
    require(!offline || job.simulated || !job.start,
            "Hardware opening is disabled in offline-test mode");
    auto &route_recovery = recovery[direction_id(job.direction)];
    if (!job.automatic && !job.force && !job.simulated_error && job.start &&
        !route_recovery.pending) {
      bool unchanged = job.direction == "output" ? output && output_selection == job.endpoint &&
                                                       api.bh_device_active(output)
                       : job.direction == "input"
                           ? capture && input_selection == job.endpoint && !microphone_fault &&
                                 api.bh_device_active(capture)
                           : loop_capture && !system_fault &&
                                 SUCCEEDED(static_cast<HRESULT>(api.bh_loop_error(loop_capture)));
      if (unchanged) {
        route_recovery.manual();
        finish(job, {{"done", true},
                     {"success", true},
                     {"unchanged", true},
                     {"endpoint_id", job.endpoint}});
        return;
      }
    }
    if (!job.automatic || job.force)
      route_recovery.manual();
    if (job.direction == "system") {
      void *candidate = nullptr, *converter = nullptr;
      DWORD error = 0;
      if (job.start) {
        candidate = api.bh_loop_open(GetCurrentProcessId(), &error);
        if (!candidate) {
          auto failure = static_cast<HRESULT>(error);
          Fault fault = failure == E_ACCESSDENIED                          ? Fault::permission
                        : failure == E_NOINTERFACE || failure == E_NOTIMPL ? Fault::format
                        : failure == E_OUTOFMEMORY                         ? Fault::memory
                        : failure == HRESULT_FROM_WIN32(ERROR_TIMEOUT)     ? Fault::timeout
                                                                           : Fault::endpoint;
          throw AudioFailure(fault, static_cast<int>(error), "Computer audio capture unavailable");
        }
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
      system_wanted = job.start;
      commit_lock.unlock();
      if (old && api.bh_loop_close(old) != 1)
        TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
      if (old_converter)
        api.bh_capture_free(old_converter);
      system_active = job.start;
      system_fault = false;
      recovery[2].success(seconds());
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
      if (job.start && job.simulated_error)
        throw AudioFailure(device_fault(job.simulated_error), job.simulated_error,
                           "Offline endpoint preparation failed");
      if (job.start && simulated_inventory_enabled) {
        std::lock_guard<std::mutex> lock(audio_mutex);
        bool available = std::any_of(
            simulated_endpoints.begin(), simulated_endpoints.end(), [&](const auto &item) {
              return item.direction == (render ? BH_PLAYBACK : BH_CAPTURE) &&
                     (job.endpoint.empty() ? item.is_default != 0
                                           : wide(job.endpoint) == item.endpoint_id);
            });
        if (!available)
          throw AudioFailure(Fault::endpoint, -204, "Offline endpoint is absent");
      }
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
      if (!candidate)
        throw AudioFailure(device_fault(error), error,
                           render ? "Cannot prepare playback endpoint"
                                  : "Cannot prepare microphone endpoint");
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
      int stopped = old ? api.bh_device_stop(old) : 0;
      if (stopped != 0) {
        if (candidate)
          api.bh_device_close(candidate);
        throw AudioFailure(device_fault(stopped), stopped,
                           "Previous playback endpoint did not stop; new consumer was not started");
      }
      int started = candidate ? api.bh_device_start(candidate) : 0;
      if (started != 0) {
        if (old && api.bh_device_start(old) != 0)
          bridge_errors++;
        api.bh_device_close(candidate);
        throw AudioFailure(device_fault(started), started, "New playback endpoint could not start");
      }
      output = candidate;
      output_active = job.start;
      output_selection = job.endpoint;
      output_wanted = job.start;
      if (!job.automatic)
        wanted_endpoint[0] = retry_endpoint[0] = job.endpoint;
    } else {
      int started = candidate ? api.bh_device_start(candidate) : 0;
      if (started != 0) {
        api.bh_device_close(candidate);
        throw AudioFailure(device_fault(started), started,
                           "New microphone endpoint could not start");
      }
      {
        std::lock_guard<std::mutex> lock(bridge_mutex);
        old = capture;
        capture = candidate;
        api.bh_up_clear(uplink, 0);
        microphone_active = job.start;
        microphone_fault = false;
      }
      input_selection = job.endpoint;
      capture_wanted = job.start;
      if (!job.automatic)
        wanted_endpoint[1] = retry_endpoint[1] = job.endpoint;
    }
    recovery[render ? 0 : 1].success(seconds());
    audio_results[job.id] = {{"done", true}, {"success", true}, {"endpoint_id", job.endpoint}};
    commit_lock.unlock();
    if (old)
      api.bh_device_close(old);
    finish(job, {{"done", true}, {"success", true}, {"endpoint_id", job.endpoint}});
  }
  std::string default_output, default_input;
  bool inventory_known = false;
  bool wanted_output_present = true, wanted_input_present = true;
  void cache_device_diagnostics() {
    // Only the audio-control owner dereferences stream pointers. The RPC
    // snapshot reads this copy and cannot race a hot switch or close.
    json result = json::object();
    result["recovery"] = {{"output", recovery[0].snapshot(seconds())},
                          {"microphone", recovery[1].snapshot(seconds())},
                          {"system", recovery[2].snapshot(seconds())}};
    for (auto direction : {"output", "microphone"}) {
      auto *stream = std::strcmp(direction, "output") == 0 ? output : capture;
      BhDeviceStats stats{};
      stats.size = sizeof(stats);
      if (!stream || api.bh_device_stats(stream, &stats) != 0)
        continue;
      result[direction] = {{"sample_rate", stats.sample_rate},
                           {"channels", stats.channels},
                           {"output_rate", stats.output_rate},
                           {"conversion", stats.conversion},
                           {"active", stats.active},
                           {"callbacks", stats.callbacks},
                           {"frames", stats.frames},
                           {"errors", stats.errors},
                           {"max_callback_us", stats.max_callback_us},
                           {"max_callback_gap_us", api.bh_device_timing(stream, 0)},
                           {"max_callback_excess_us", api.bh_device_timing(stream, 1)},
                           {"callback_gap_events", api.bh_device_timing(stream, 2)},
                           {"notifications", stats.notifications},
                           {"dropped_frames", stats.dropped_frames},
                           {"queued_frames", stats.queued_frames},
                           {"filter_latency_frames", stats.filter_latency_frames}};
    }
    std::lock_guard<std::mutex> lock(diagnostic_mutex);
    device_diagnostics = std::move(result);
  }
  void check_devices() {
    auto now = seconds();
    if (!offline) {
      output_active = output && api.bh_device_active(output);
      microphone_active = capture && api.bh_device_active(capture);
    }
    recovery[0].healthy(now, output_active);
    recovery[1].healthy(now, microphone_active);
    recovery[2].healthy(now, system_active && !system_fault);
    if (offline && !simulated_inventory_enabled)
      return;
    if (loop_capture && FAILED(static_cast<HRESULT>(api.bh_loop_error(loop_capture))))
      system_fault = true;
    bool pending_system = false;
    uint64_t system_ticket = 0;
    {
      std::lock_guard<std::mutex> lock(audio_mutex);
      system_ticket = latest_job[2];
      pending_system = std::any_of(audio_jobs.begin(), audio_jobs.end(),
                                   [](const auto &j) { return j.direction == "system"; });
    }
    if (system_wanted && !quiesced && !pending_system &&
        (system_fault || (recovery[2].pending && recovery[2].automatic)) &&
        recovery[2].ready(now)) {
      device_reopens++;
      schedule("system", "", true, voice_epoch, false, 0, system_ticket, true);
    }
    if (!output_wanted && !capture_wanted)
      return;
    std::vector<BhEndpoint> endpoints;
    std::array<int, 2> simulated_errors{};
    if (offline) {
      std::lock_guard<std::mutex> lock(audio_mutex);
      endpoints = simulated_endpoints;
      simulated_errors = simulated_prepare_errors;
    } else {
      uint32_t count = 0;
      int result = api.bh_device_inventory(context, nullptr, 0, &count);
      if (result != 0 && result != BH_DEVICE_BUFFER_SMALL)
        return;
      if (count > 4096)
        return;
      endpoints.resize(count);
      if (api.bh_device_inventory(context, endpoints.data(), count, &count) != 0)
        return;
    }
    std::string out, in;
    bool output_present = wanted_endpoint[0].empty(), input_present = wanted_endpoint[1].empty();
    for (const auto &e : endpoints) {
      auto identity = utf8(e.endpoint_id);
      if (e.direction == BH_PLAYBACK && identity == wanted_endpoint[0])
        output_present = true;
      if (e.direction == BH_CAPTURE && identity == wanted_endpoint[1])
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
    if (inventory_known && (output_changed || output_present != wanted_output_present))
      recovery[0].manual();
    if (inventory_known && (input_changed || input_present != wanted_input_present))
      recovery[1].manual();
    wanted_output_present = output_present;
    wanted_input_present = input_present;
    default_output = out;
    default_input = in;
    inventory_known = true;
    bool pending_output = false, pending_input = false;
    uint64_t output_ticket = 0, input_ticket = 0;
    auto selected_output = output_present ? wanted_endpoint[0] : std::string();
    auto selected_input = input_present ? wanted_endpoint[1] : std::string();
    {
      std::lock_guard<std::mutex> lock(audio_mutex);
      output_ticket = latest_job[0];
      input_ticket = latest_job[1];
      retry_endpoint = {selected_output, selected_input};
      pending_output = std::any_of(audio_jobs.begin(), audio_jobs.end(),
                                   [](const auto &j) { return j.direction == "output"; });
      pending_input = std::any_of(audio_jobs.begin(), audio_jobs.end(),
                                  [](const auto &j) { return j.direction == "input"; });
    }
    if (output_wanted && !quiesced && !pending_output && recovery[0].ready(now) &&
        (!output_active || output_changed || selected_output != output_selection ||
         (recovery[0].pending && recovery[0].automatic))) {
      device_reopens++;
      schedule("output", selected_output, true, 0, offline, 0, output_ticket, true,
               simulated_errors[0]);
    }
    if (capture_wanted && !quiesced && !pending_input && recovery[1].ready(now) &&
        (!microphone_active || microphone_fault || input_changed ||
         selected_input != input_selection || (recovery[1].pending && recovery[1].automatic))) {
      device_reopens++;
      schedule("input", selected_input, true, 0, offline, 0, input_ticket, true,
               simulated_errors[1]);
    }
  }
  void bridge_loop() {
    Priority priority;
    std::array<float, 2048> pcm{};
    std::array<float, 4800> system{};
    while (!quitting) {
      test_pause(bridge_stall);
      {
        std::lock_guard<std::mutex> lock(bridge_mutex);
        if (capture) {
          uint64_t position = 0, arrival = 0;
          if (api.bh_device_clock(capture, &position, &arrival) > 0)
            api.bh_up_observe(uplink, 0, position, arrival);
          for (unsigned batch = 0; batch < 8; batch++) {
            int n = api.bh_device_read_f32(capture, pcm.data(), 1024);
            if (n <= 0) {
              if (n < 0) {
                bridge_errors++;
                microphone_fault = true;
              }
              break;
            }
            if (voice_handle != 0xffff)
              api.bh_up_write_f32(uplink, 0, pcm.data(), n);
            bridge_frames += n;
            // A large endpoint callback may contain several read quanta.
            // Re-arm a bounded drain rather than wait for another callback.
            if (batch == 7)
              SetEvent(bridge_wake);
          }
        }
        if (loop_capture && loop_converter) {
          uint64_t position = 0, arrival = 0;
          if (api.bh_loop_clock(loop_capture, &position, &arrival) > 0)
            api.bh_up_observe(uplink, 1, position, arrival);
          int n = api.bh_loop_read(loop_capture, system.data(), 4800);
          if (n < 0) {
            bridge_errors++;
            system_fault = true;
          }
          if (n > 0) {
            api.bh_capture_push(loop_converter, system.data(), n);
            while ((n = api.bh_capture_read_f32(loop_converter, pcm.data(), 1024)) > 0) {
              if (voice_handle != 0xffff && mix_enabled)
                api.bh_up_write_f32(uplink, 1, pcm.data(), n);
              system_frames += n;
            }
          }
        }
      }
      SetEvent(voice_wake);
      WaitForSingleObject(bridge_wake, INFINITE);
    }
  }

public:
  Fault classify_fault(int code) { return device_fault(code); }
  void observe_offline_capture(uint32_t frames) {
    require(offline, "Offline-test mode required");
    bridge_frames += frames;
  }
  EndpointActor(Api &a, const fs::path &path, void *mix, void *up, bool test,
                const std::atomic<bool> &paused, const std::atomic<uint32_t> &epoch,
                const std::atomic<uint32_t> &handle, std::atomic<bool> &mixed, HANDLE wake,
                std::atomic<uint64_t> &stalls)
      : api(a), assets(path), mixer(mix), uplink(up), offline(test), quiesced(paused),
        voice_epoch(epoch), voice_handle(handle), mix_enabled(mixed), voice_wake(wake),
        injected_stalls(stalls) {
    try {
      int error = 0;
      context = api.bh_device_context_create((assets / L"bluehead_audio.dll").c_str(), &error);
      require(context, "WASAPI context could not initialize");
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
    } catch (...) {
      stop_and_join();
      throw;
    }
  }
  ~EndpointActor() { stop_and_join(); }

private:
  void stop_and_join() {
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
    if (context) {
      api.bh_device_context_close(context);
      context = nullptr;
    }
  }

public:
  void clear_uplink() {
    std::lock_guard<std::mutex> lock(bridge_mutex);
    api.bh_up_clear(uplink, -1);
  }
  void stall_capture(uint32_t ms) {
    bridge_stall = ms;
    SetEvent(bridge_wake);
  }
  std::optional<json> command(const json &request) {
    std::string op = request.at("op");
    if (offline && (op == "audio_switch" || op == "system_audio"))
      throw std::runtime_error("Hardware opening is disabled in offline-test mode");
    if (op == "test_audio_switch") {
      require(offline, "Offline-test mode required");
      std::string direction = request.at("direction");
      auto delay = request.value("delay_ms", 0u);
      require((direction == "input" || direction == "output") && delay <= 2000,
              "Invalid offline audio job");
      return schedule(direction, request.value("endpoint_id", std::string()),
                      request.value("start", true), request.value("voice_epoch", 0u), true, delay,
                      UINT64_MAX, false, request.value("error_code", 0),
                      request.value("force", false));
    }
    if (op == "test_inventory") {
      require(offline, "Offline-test mode required");
      const auto &items = request.at("endpoints");
      require(items.is_array() && items.size() <= 16, "Invalid endpoint inventory");
      std::vector<BhEndpoint> next;
      for (const auto &item : items) {
        auto id = wide(item.at("id"));
        require(!id.empty() && id.size() < 64, "Invalid endpoint identity");
        BhEndpoint endpoint{};
        endpoint.size = sizeof(endpoint);
        endpoint.direction =
            item.value("direction", std::string("output")) == "output" ? BH_PLAYBACK : BH_CAPTURE;
        endpoint.is_default = item.value("default", false);
        std::copy(id.begin(), id.end(), endpoint.endpoint_id);
        next.push_back(endpoint);
      }
      {
        std::lock_guard<std::mutex> lock(audio_mutex);
        simulated_endpoints = std::move(next);
        simulated_prepare_errors = {request.value("output_error", 0),
                                    request.value("input_error", 0)};
        simulated_inventory_enabled = true;
      }
      audio_condition.notify_one();
      return true;
    }
    if (op == "test_routes") {
      require(offline, "Offline-test mode required");
      std::lock_guard<std::mutex> lock(audio_mutex);
      return json{{"output", output_selection},
                  {"input", input_selection},
                  {"wanted_output", wanted_endpoint[0]},
                  {"wanted_input", wanted_endpoint[1]}};
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
                      request.value("start", true), generation, false, 0, UINT64_MAX, false, 0,
                      request.value("force", false));
    }
    if (op == "audio_retry") {
      require(!quiesced, "Receiver is stopping");
      std::vector<std::tuple<std::string, std::string, uint64_t>> routes;
      {
        std::lock_guard<std::mutex> lock(audio_mutex);
        auto available = [&](const char *direction) {
          return !preparing_audio[direction_id(direction)] &&
                 std::none_of(audio_jobs.begin(), audio_jobs.end(),
                              [&](const auto &job) { return job.direction == direction; });
        };
        if (output_wanted && available("output"))
          routes.emplace_back("output", retry_endpoint[0], latest_job[0]);
        if (capture_wanted && available("input"))
          routes.emplace_back("input", retry_endpoint[1], latest_job[1]);
        if (system_wanted && available("system"))
          routes.emplace_back("system", "", latest_job[2]);
      }
      json tickets = json::array();
      for (const auto &route : routes)
        tickets.push_back(schedule(std::get<0>(route), std::get<1>(route), true, 0, offline, 0,
                                   std::get<2>(route), true, 0, true));
      return tickets;
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
        return json{{"done", true}, {"success", true}, {"cancelled", true}};
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
    return std::nullopt;
  }
  json snapshot() {
    json devices;
    {
      std::lock_guard<std::mutex> lock(diagnostic_mutex);
      devices = device_diagnostics;
    }
    return {{"devices", devices},
            {"microphone_active", microphone_active.load()},
            {"system_active", system_active.load()},
            {"output_active", output_active.load()},
            {"microphone_frames", bridge_frames.load()},
            {"system_frames", system_frames.load()},
            {"bridge_errors", bridge_errors.load()},
            {"device_reopens", device_reopens.load()}};
  }
};
} // namespace bh
