/* Experimental LDAC stays isolated. Audio crosses native shared memory only. */
#pragma once
#include "runtime_common.hpp"
namespace bh {
struct LdacShared {
  uint32_t magic = 0x42484c44, operation = 0, size = 0, frames = 0;
  int32_t result = 0;
  uint32_t rate = 48000, channels = 2;
  std::array<uint64_t, 10> stats{};
  std::array<uint8_t, 65535> packet{};
  std::array<float, 48064 * 2> pcm{};
};
inline fs::path executable() {
  std::wstring path(32768, 0);
  DWORD n = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
  require(n && n < path.size(), "Native executable path unavailable");
  path.resize(n);
  return fs::path(path);
}
class LdacWorker {
  Handle mapping, request, done;
  Process child;
  LdacShared *shared = nullptr;

public:
  LdacWorker(const fs::path &assets, uint32_t rate, uint32_t channels) {
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    mapping.reset(CreateFileMappingW(INVALID_HANDLE_VALUE, &security, PAGE_READWRITE, 0,
                                     sizeof(LdacShared), nullptr));
    request.reset(CreateEventW(&security, FALSE, FALSE, nullptr));
    done.reset(CreateEventW(&security, FALSE, FALSE, nullptr));
    require(mapping && request && done, "LDAC native channel unavailable");
    shared = static_cast<LdacShared *>(
        MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(LdacShared)));
    require(shared, "LDAC shared memory unavailable");
    new (shared) LdacShared();
    shared->rate = rate;
    shared->channels = channels;
    auto command = quote(executable().wstring()) + L" --ldac-worker " +
                   std::to_wstring(reinterpret_cast<uintptr_t>(mapping.value)) + L" " +
                   std::to_wstring(reinterpret_cast<uintptr_t>(request.value)) + L" " +
                   std::to_wstring(reinterpret_cast<uintptr_t>(done.value)) + L" --assets " +
                   quote(assets.wstring());
    try {
      child = spawn(command, {mapping, request, done});
      HANDLE wait[] = {done, child.process};
      require(WaitForMultipleObjects(2, wait, FALSE, 3000) == WAIT_OBJECT_0 && shared->result == 0,
              "LDAC worker initialization failed");
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
  ~LdacWorker() {
    child.job.reset();
    if (child.process)
      WaitForSingleObject(child.process, 1000);
    if (shared)
      UnmapViewOfFile(shared);
  }
  uint32_t feed(const uint8_t *p, uint32_t n, float *out, uint64_t *stats) {
    require(n && n <= shared->packet.size(), "Invalid LDAC packet");
    std::memcpy(shared->packet.data(), p, n);
    shared->size = n;
    shared->operation = 1;
    MemoryBarrier();
    SetEvent(request);
    HANDLE wait[] = {done, child.process};
    if (WaitForMultipleObjects(2, wait, FALSE, 500) != WAIT_OBJECT_0) {
      child.job.reset();
      throw std::runtime_error("LDAC worker stalled");
    }
    MemoryBarrier();
    require(shared->frames <= 48064, "LDAC worker ABI violation");
    if (shared->result < 0)
      shared->frames = 0;
    std::memcpy(out, shared->pcm.data(), shared->frames * 2 * sizeof(float));
    std::memcpy(stats, shared->stats.data(), shared->stats.size() * sizeof(uint64_t));
    return shared->frames;
  }
};
inline int ldac_worker(HANDLE mapping, HANDLE request, HANDLE done, const fs::path &assets) {
  auto *s = static_cast<LdacShared *>(
      MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(LdacShared)));
  if (!s)
    return 2;
  void *decoder = nullptr;
  try {
    require(s->magic == 0x42484c44 &&
                (s->rate == 44100 || s->rate == 48000 || s->rate == 88200 || s->rate == 96000) &&
                (s->channels == 1 || s->channels == 2),
            "LDAC shared ABI mismatch");
    Module library(assets / L"bluehead_ldac_media.dll");
    auto create = library.get<void *(*)(int, int, int)>("bh_ldac_media_create");
    auto destroy = library.get<void (*)(void *)>("bh_ldac_media_destroy");
    auto feed = library.get<int (*)(void *, const uint8_t *, uint32_t)>("bh_ldac_media_feed");
    auto read = library.get<uint32_t (*)(void *, float *, uint32_t)>("bh_ldac_media_read");
    auto value = library.get<uint64_t (*)(void *, int)>("bh_ldac_media_value");
    decoder = create(s->rate, s->channels, s->rate);
    require(decoder, "LDAC codec unavailable");
    s->result = 0;
    MemoryBarrier();
    SetEvent(done);
    while (WaitForSingleObject(request, INFINITE) == WAIT_OBJECT_0) {
      MemoryBarrier();
      if (s->operation != 1 || !s->size || s->size > s->packet.size())
        break;
      s->result = feed(decoder, s->packet.data(), s->size);
      s->frames = read(decoder, s->pcm.data(), 48064);
      for (int i = 0; i < 10; i++)
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
