/* Offline 26-hour virtual PCM clock test. Apache-2.0. No device is opened. */
#include "runtime_api.hpp"
#include "audio_timeline.h"
#include <fstream>
#include <iostream>
using namespace bh;
struct ClockCase {
  void *mixer = nullptr, *route = nullptr;
  int input = -1, ppm = 0;
  uint64_t packet = 1, generated = 0, minimum = UINT64_MAX, maximum = 0, crossings = 0;
  double square = 0, last = 0, stereo_error = 0;
  uint64_t measured = 0;
  std::vector<float> pattern;
  std::vector<float> input_pcm, output;
};
int wmain(int count, wchar_t **args) {
  try {
    require(count >= 3,
            "Usage: clock-stress assets report [virtual-seconds] [source-rate] [output-rate]");
    fs::path assets = fs::absolute(args[1]), report = fs::absolute(args[2]);
    uint64_t duration = count > 3 ? std::stoull(args[3]) : 93600;
    require(duration >= 600 && duration <= 172800, "Choose 600–172800 virtual seconds");
    uint32_t source_rate = count > 4 ? std::stoul(args[4]) : 48000;
    uint32_t output_rate = count > 5 ? std::stoul(args[5]) : source_rate;
    require(source_rate >= 8000 && source_rate <= 384000 && source_rate % 100 == 0 &&
                output_rate >= 8000 && output_rate <= 384000 && output_rate % 100 == 0,
            "Choose supported rates with integral 10ms quanta");
    uint32_t source_quantum = source_rate / 100, output_quantum = output_rate / 100;
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    AddDllDirectory(assets.c_str());
    AddDllDirectory(assets.parent_path().parent_path().c_str());
    Api api(assets);
    auto write_at = api.audio.get<void (*)(void *, int, const float *, uint32_t, uint64_t)>(
        "bh_mix_test_write_at");
    auto route_create = api.audio.get<void *(*)(void *, uint32_t)>("bh_mix_route_create");
    auto route_free = api.audio.get<void (*)(void *)>("bh_mix_route_free");
    auto route_render =
        api.audio.get<int (*)(void *, float *, uint32_t, float)>("bh_mix_render_route");
    std::array<ClockCase, 2> cases;
    for (size_t i = 0; i < cases.size(); i++) {
      auto &c = cases[i];
      c.ppm = i ? 500 : -500;
      c.mixer = api.bh_mix_create();
      require(c.mixer, "Mixer allocation failed");
      c.input = api.bh_mix_add(c.mixer, 2880, 8640);
      require(c.input >= 0, "Input allocation failed");
      require(api.bh_mix_format(c.mixer, c.input, source_rate) == 0, "Source format setup failed");
      c.route = route_create(c.mixer, output_rate);
      require(c.route, "Output format setup failed");
      c.input_pcm.resize(source_quantum * 2);
      c.output.resize(output_quantum * 2);
      require(api.bh_mix_continuity(c.mixer, c.input, 1, 1440, 5760) == 0,
              "Continuity setup failed");
      require(api.bh_mix_timeline(c.mixer, c.input, BHT_MEDIA) == 0,
              "Presentation timeline setup failed");
      // Exact 1 kHz at 47976/48024 physical samples/s; 125 cycles per pattern.
      uint32_t period =
          static_cast<uint32_t>(std::lround(source_rate * (1 + c.ppm / 1000000.0) / 8));
      c.pattern.resize(period);
      for (uint32_t j = 0; j < period; j++)
        c.pattern[j] = .2f * std::sin(j * 6.283185307179586 * 125 / period);
    }
    uint64_t origin = ticks();
    auto write_report = [&](uint64_t block, bool done) {
      json result = {
          {"scope",
           "Actual native stereo PCM render under virtual source clocks; no USB/audio opened"},
          {"version", version},
          {"source_rate", source_rate},
          {"output_rate", output_rate},
          {"done", done},
          {"virtual_seconds", block / 100.0},
          {"requested_seconds", duration},
          {"wall_seconds", (ticks() - origin) / static_cast<double>(frequency())}};
      json entries = json::array();
      bool passed = true;
      for (auto &c : cases) {
        auto dropped = api.bh_mix_value(c.mixer, c.input, 1),
             underruns = api.bh_mix_value(c.mixer, c.input, 3);
        int64_t drift = static_cast<int64_t>(api.bh_mix_value(c.mixer, c.input, 2)) - 1000;
        double rms = c.measured ? std::sqrt(c.square / (c.measured * 2)) : 0;
        double hz = c.measured ? c.crossings * static_cast<double>(output_rate) / c.measured : 0;
        bool good = !dropped && !underruns && std::abs(drift - c.ppm) < 70 && c.minimum > 0 &&
                    c.maximum < 5760;
        if (done)
          good =
              good && rms > .135 && rms < .147 && std::abs(hz - 1000) < 1 && c.stereo_error < 1e-6;
        passed = passed && good;
        entries.push_back({{"ppm", c.ppm},
                           {"rendered_frames", block * output_quantum},
                           {"drift_ppm", drift},
                           {"dropped_frames", dropped},
                           {"underruns", underruns},
                           {"callback_period_searches", api.bh_mix_value(c.mixer, c.input, 17)},
                           {"queue_min", c.minimum == UINT64_MAX ? 0 : c.minimum},
                           {"queue_max", c.maximum},
                           {"tone_rms", rms},
                           {"tone_hz", hz},
                           {"stereo_error", c.stereo_error},
                           {"passed", good}});
      }
      result["cases"] = entries;
      result["passed"] = done && passed;
      fs::create_directories(report.parent_path());
      auto temporary = report;
      temporary += L".tmp";
      {
        std::ofstream stream(temporary);
        stream << result.dump(2) << '\n';
        require(stream.good(), "Clock report write failed");
      }
      require(MoveFileExW(temporary.c_str(), report.c_str(),
                          MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0,
              "Clock report commit failed");
      return passed;
    };
    uint64_t total = duration * 100;
    for (uint64_t block = 0; block < total; block++) {
      double now = block * .01;
      for (auto &c : cases) {
        while (c.packet * .01 / (1 + c.ppm / 1000000.0) + .003 * std::sin(c.packet * 1.113) <=
               now) {
          for (size_t j = 0; j < source_quantum; j++) {
            float v = c.pattern[(c.generated + j) % c.pattern.size()];
            c.input_pcm[j * 2] = v;
            c.input_pcm[j * 2 + 1] = -v;
          }
          double arrival =
              c.packet * .01 / (1 + c.ppm / 1000000.0) + .003 * std::sin(c.packet * 1.113);
          api.bh_mix_observe(c.mixer, c.input, bht_scale(c.generated, source_rate, BHT_RATE),
                             origin + static_cast<uint64_t>(arrival * frequency()));
          write_at(c.mixer, c.input, c.input_pcm.data(), source_quantum,
                   origin + static_cast<uint64_t>(arrival * frequency()));
          c.generated += source_quantum;
          c.packet++;
        }
        require(route_render(c.route, c.output.data(), output_quantum, 1) == 1,
                "Native render failed");
        if (block > 6000) {
          uint64_t queued = api.bh_mix_value(c.mixer, c.input, 0);
          c.minimum = std::min(c.minimum, queued);
          c.maximum = std::max(c.maximum, queued);
        }
        if (block + 1000 >= total) {
          for (size_t j = 0; j < output_quantum; j++) {
            double a = c.output[j * 2], b = c.output[j * 2 + 1];
            require(std::isfinite(a) && std::isfinite(b), "Nonfinite PCM");
            c.square += a * a + b * b;
            c.stereo_error = std::max(c.stereo_error, std::abs(a + b));
            if (a > 0 && c.last <= 0)
              c.crossings++;
            c.last = a;
          }
          c.measured += output_quantum;
        }
      }
      if (block && block % 360000 == 0)
        write_report(block, false);
    }
    bool passed = write_report(total, true);
    for (auto &c : cases) {
      route_free(c.route);
      api.bh_mix_free(c.mixer);
    }
    std::cout << (passed ? "Virtual clock stress passed\n" : "Virtual clock stress failed\n");
    return passed ? 0 : 1;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
}
