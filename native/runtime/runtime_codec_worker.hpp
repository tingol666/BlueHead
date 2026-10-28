/* Per-peer native codec process. Audio crosses bounded shared memory only.
 * Never owns USB, a microphone or a playback endpoint. Apache-2.0. */
#pragma once
#include "runtime_common.hpp"
namespace bh {
struct CodecShared {
  uint32_t magic = 0x42484346, layout_version = 2, layout_bytes = 0;
  uint32_t operation = 0, size = 0, frames = 0;
  int32_t result = 0;
  uint32_t rate = 48000, channels = 2, codec = 5, depth = 16, test_fault = 0;
  uint32_t free_frames = 0, remaining_frames = 0, clock_uncertain = 0;
  std::array<uint64_t, 24> stats{};
  std::array<uint8_t, 65535> packet{};
  std::array<float, 48064 * 2> pcm{};
  std::array<uint8_t, 48064> fence{};
};
inline fs::path executable() {
  std::wstring path(32768, 0);
  DWORD n = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
  require(n && n < path.size(), "Native executable path unavailable");
  path.resize(n);
  return fs::path(path);
}
class IsolatedMediaWorker {
  Handle mapping, request, done;
  Process child;
  CodecShared *shared = nullptr;

public:
  IsolatedMediaWorker(const fs::path &assets, uint32_t rate, uint32_t channels, uint32_t codec = 5, uint32_t depth = 16) {
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    mapping.reset(CreateFileMappingW(INVALID_HANDLE_VALUE, &security, PAGE_READWRITE, 0,
                                     sizeof(CodecShared), nullptr));
    request.reset(CreateEventW(&security, FALSE, FALSE, nullptr));
    done.reset(CreateEventW(&security, FALSE, FALSE, nullptr));
    require(mapping && request && done, "Isolated codec channel unavailable");
    shared = static_cast<CodecShared *>(
        MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(CodecShared)));
    require(shared, "Codec shared memory unavailable");
    new (shared) CodecShared();
    shared->layout_bytes = sizeof(CodecShared);
    shared->rate = rate;
    shared->channels = channels;
    shared->codec = codec; shared->depth = depth;
    auto command = quote(executable().wstring()) + L" --codec-worker " +
                   std::to_wstring(reinterpret_cast<uintptr_t>(mapping.value)) + L" " +
                   std::to_wstring(reinterpret_cast<uintptr_t>(request.value)) + L" " +
                   std::to_wstring(reinterpret_cast<uintptr_t>(done.value)) + L" --assets " +
                   quote(assets.wstring());
    try {
      child = spawn(command, {mapping, request, done});
      HANDLE wait[] = {done, child.process};
      require(WaitForMultipleObjects(2, wait, FALSE, 3000) == WAIT_OBJECT_0 && shared->result == 0,
              "Codec worker initialization failed");
    } catch (...) {
      if (shared) {
        UnmapViewOfFile(shared);
        shared = nullptr;
      }
      throw;
    }
    SetHandleInformation(mapping, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(request, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(done, HANDLE_FLAG_INHERIT, 0);
  }
  ~IsolatedMediaWorker() {
    child.job.reset();
    if (child.process)
      WaitForSingleObject(child.process, 1000);
    if (shared)
      UnmapViewOfFile(shared);
  }
  void inject_fault(uint32_t mode) { shared->test_fault=mode; } // Offline caller only.
  uint32_t feed(const uint8_t *p, uint32_t n, float *out, uint8_t *fence, uint64_t *stats,
                uint32_t free_frames, uint32_t remaining_frames, uint32_t &uncertain) {
    require(n && n <= shared->packet.size(), "Invalid isolated codec packet");
    std::memcpy(shared->packet.data(), p, n);
    shared->size = n;
    shared->free_frames = free_frames; shared->remaining_frames = remaining_frames;
    shared->operation = 1;
    MemoryBarrier();
    SetEvent(request);
    HANDLE wait[] = {done, child.process};
    if (WaitForMultipleObjects(2, wait, FALSE, 500) != WAIT_OBJECT_0) {
      child.job.reset();
      throw std::runtime_error("Codec worker stalled");
    }
    MemoryBarrier();
    require(shared->magic == 0x42484346 && shared->layout_version == 2 &&
                shared->layout_bytes == sizeof(CodecShared) && shared->frames <= 48064,
            "Codec worker ABI violation");
    if (shared->result < 0)
      shared->frames = 0;
    std::memcpy(out, shared->pcm.data(), shared->frames * 2 * sizeof(float));
    std::memcpy(fence, shared->fence.data(), shared->frames);
    uncertain = shared->clock_uncertain;
    std::memcpy(stats, shared->stats.data(), shared->stats.size() * sizeof(uint64_t));
    return shared->frames;
  }
};
inline int codec_worker(HANDLE mapping, HANDLE request, HANDLE done, const fs::path &assets) {
  auto *s = static_cast<CodecShared *>(
      MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(CodecShared)));
  if (!s)
    return 2;
  void *decoder = nullptr;
  try {
    require(s->magic == 0x42484346 && s->layout_version == 2 &&
                s->layout_bytes == sizeof(CodecShared) &&
                (s->codec == 5 || s->codec == 6) &&
                (s->rate == 44100 || s->rate == 48000 || s->rate == 96000 ||
                 (s->codec == 5 && s->rate == 88200) || (s->codec == 6 && s->rate == 192000)) &&
                (s->codec != 6 || s->depth == 16 || s->depth == 24) &&
                (s->channels == 1 || s->channels == 2),
            "Codec shared ABI mismatch");
    Module library(assets / (s->codec == 5 ? L"bluehead_ldac_media.dll" : L"bluehead_lhdc5.dll"));
    const std::string prefix = s->codec == 5 ? "bh_ldac_media_" : "bh_lhdc5_media_";
    auto create = library.get<void *(*)(int, int, int)>((prefix + "create").c_str());
    auto destroy = library.get<void (*)(void *)>((prefix + "destroy").c_str());
    auto feed = library.get<int (*)(void *, const uint8_t *, uint32_t)>((prefix + "feed").c_str());
    auto read = library.get<uint32_t (*)(void *, float *, uint8_t *, uint32_t)>((prefix + "read_fenced").c_str());
    auto budget = library.get<void (*)(void *, uint32_t, uint32_t)>((prefix + "gap_context").c_str());
    auto confidence = library.get<int (*)(void *)>((prefix + "drain_confidence").c_str());
    auto value = library.get<uint64_t (*)(void *, int)>((prefix + "value").c_str());
    decoder = create(s->rate, s->channels, s->codec == 5 ? s->rate : s->depth);
    require(decoder, "Isolated codec unavailable");
    s->result = 0;
    MemoryBarrier();
    SetEvent(done);
    Priority priority;
    while (WaitForSingleObject(request, INFINITE) == WAIT_OBJECT_0) {
      MemoryBarrier();
      auto fault=s->test_fault;s->test_fault=0;
      if(fault==1) ExitProcess(77);
      if(fault==2) Sleep(2000);
      if (s->magic != 0x42484346 || s->layout_version != 2 || s->layout_bytes != sizeof(CodecShared) ||
          s->operation != 1 || !s->size || s->size > s->packet.size())
        break;
      budget(decoder, s->free_frames, s->remaining_frames);
      s->result = feed(decoder, s->packet.data(), s->size);
      s->clock_uncertain = confidence(decoder);
      s->frames = read(decoder, s->pcm.data(), s->fence.data(), 48064);
      for (int i = 0; i < 24; i++)
        s->stats[i] = value(decoder, i);
      MemoryBarrier();
      SetEvent(done);
    }
    destroy(decoder);
    decoder = nullptr;
  } catch (const std::exception &) {
    s->result = -1;
    MemoryBarrier();
    SetEvent(done);
  }
  UnmapViewOfFile(s);
  return decoder ? 2 : 0;
}
} // namespace bh
