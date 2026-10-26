/* BlueHead native runtime: bounded queues and Windows ownership. Apache-2.0. */
#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "runtime_version.h"
#include "json.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <avrt.h>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <memory>
#include <mutex>
#include <objbase.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "audio_float.h"
namespace bh {
using json = nlohmann::json;
namespace fs = std::filesystem;
inline constexpr const char *version = BH_RUNTIME_VERSION;
struct Handle {
  HANDLE value = nullptr;
  Handle() = default;
  explicit Handle(HANDLE h) : value(h) {}
  Handle(const Handle &) = delete;
  Handle &operator=(const Handle &) = delete;
  Handle(Handle &&o) noexcept : value(o.value) { o.value = nullptr; }
  Handle &operator=(Handle &&o) noexcept {
    if (this != &o) {
      reset();
      value = o.value;
      o.value = nullptr;
    }
    return *this;
  }
  ~Handle() { reset(); }
  void reset(HANDLE h = nullptr) {
    if (value && value != INVALID_HANDLE_VALUE)
      CloseHandle(value);
    value = h;
  }
  explicit operator bool() const { return value && value != INVALID_HANDLE_VALUE; }
  operator HANDLE() const { return value; }
};
class Priority {
  HANDLE task = nullptr;
  unsigned floating = bh_float_enter();

public:
  explicit Priority(const wchar_t *name = L"Audio") {
    DWORD i = 0;
    task = AvSetMmThreadCharacteristicsW(name, &i);
  }
  ~Priority() {
    if (task)
      AvRevertMmThreadCharacteristics(task);
    bh_float_leave(floating);
  }
};
inline uint64_t ticks() {
  LARGE_INTEGER t;
  QueryPerformanceCounter(&t);
  return static_cast<uint64_t>(t.QuadPart);
}
inline uint64_t frequency() {
  static const uint64_t f = []() {
    LARGE_INTEGER v;
    QueryPerformanceFrequency(&v);
    return static_cast<uint64_t>(v.QuadPart);
  }();
  return f;
}
inline double seconds() { return static_cast<double>(ticks()) / frequency(); }
inline uint64_t elapsed_ticks(uint64_t now, uint64_t stamp) {
  return now >= stamp ? now - stamp : 0;
}
inline uint64_t age_ticks(uint64_t stamp) { return elapsed_ticks(ticks(), stamp); }
inline std::wstring wide(const std::string &s) {
  if (s.empty())
    return {};
  int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()),
                              nullptr, 0);
  if (!n)
    throw std::runtime_error("Invalid UTF-8");
  std::wstring r(n, 0);
  MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), r.data(),
                      n);
  return r;
}
inline std::string utf8(const std::wstring &s) {
  if (s.empty())
    return {};
  int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0, nullptr,
                              nullptr);
  std::string r(n, 0);
  WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), r.data(), n, nullptr,
                      nullptr);
  return r;
}
inline std::string hex(const uint8_t *p, size_t n) {
  static constexpr char h[] = "0123456789abcdef";
  std::string r(n * 2, 0);
  for (size_t i = 0; i < n; i++) {
    r[i * 2] = h[p[i] >> 4];
    r[i * 2 + 1] = h[p[i] & 15];
  }
  return r;
}
inline std::vector<uint8_t> unhex(const std::string &s, size_t max = 65540) {
  if (s.size() % 2 || s.size() / 2 > max)
    throw std::runtime_error("Invalid bounded packet");
  std::vector<uint8_t> r(s.size() / 2);
  auto v = [](char c) {
    if (c >= '0' && c <= '9')
      return c - '0';
    if (c >= 'a' && c <= 'f')
      return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
      return c - 'A' + 10;
    throw std::runtime_error("Invalid hex packet");
  };
  for (size_t i = 0; i < r.size(); i++)
    r[i] = static_cast<uint8_t>(v(s[i * 2]) * 16 + v(s[i * 2 + 1]));
  return r;
}
inline uint16_t le16(const uint8_t *p) { return static_cast<uint16_t>(p[0] | p[1] << 8); }
inline void put16(uint8_t *p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
}
inline void require(bool ok, const char *m) {
  if (!ok)
    throw std::runtime_error(m);
}
/* One producer/consumer. Physical USB and offline replay are exclusive. */
template <class T, size_t N> class Spsc {
  alignas(64) std::atomic<uint64_t> write{0};
  alignas(64) std::atomic<uint64_t> read{0};
  std::unique_ptr<T[]> data = std::make_unique<T[]>(N);

public:
  bool push(const T &v) {
    auto w = write.load(std::memory_order_relaxed);
    if (w - read.load(std::memory_order_acquire) >= N)
      return false;
    data[w % N] = v;
    write.store(w + 1, std::memory_order_release);
    return true;
  }
  bool pop(T &v) {
    auto r = read.load(std::memory_order_relaxed);
    if (r == write.load(std::memory_order_acquire))
      return false;
    v = data[r % N];
    read.store(r + 1, std::memory_order_release);
    return true;
  }
  size_t size() const {
    return static_cast<size_t>(write.load(std::memory_order_acquire) -
                               read.load(std::memory_order_acquire));
  }
};
struct Packet {
  uint32_t size = 0, epoch = 0;
  uint64_t arrival = 0;
  std::array<uint8_t, 65540> data;
  Packet() = default;
  Packet(const Packet &other) { *this = other; }
  Packet &operator=(const Packet &other) {
    size = std::min<uint32_t>(other.size, data.size());
    epoch = other.epoch;
    arrival = other.arrival;
    if (this != &other)
      std::memcpy(data.data(), other.data.data(), size);
    return *this;
  }
};
struct SmallPacket {
  uint32_t size = 0, epoch = 0, status = 0;
  uint64_t arrival = 0;
  std::array<uint8_t, 4096> data;
  SmallPacket() = default;
  SmallPacket(const SmallPacket &other) { *this = other; }
  SmallPacket &operator=(const SmallPacket &other) {
    size = std::min<uint32_t>(other.size, data.size());
    epoch = other.epoch;
    status = other.status;
    arrival = other.arrival;
    if (this != &other)
      std::memcpy(data.data(), other.data.data(), size);
    return *this;
  }
};
struct Module {
  HMODULE handle = nullptr;
  explicit Module(const fs::path &p) {
    handle = LoadLibraryExW(p.c_str(), nullptr,
                            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!handle)
      throw std::runtime_error("Native library missing: " + p.filename().string());
  }
  ~Module() {
    if (handle)
      FreeLibrary(handle);
  }
  Module(const Module &) = delete;
  template <class T> T get(const char *name) {
    FARPROC f = GetProcAddress(handle, name);
    if (!f)
      throw std::runtime_error(std::string("Native ABI missing: ") + name);
    T r;
    static_assert(sizeof(r) == sizeof(f));
    std::memcpy(&r, &f, sizeof(f));
    return r;
  }
};
inline bool read_all(HANDLE h, void *p, uint32_t n) {
  auto *b = static_cast<uint8_t *>(p);
  while (n) {
    DWORD k = 0;
    if (!ReadFile(h, b, n, &k, nullptr) || !k)
      return false;
    b += k;
    n -= k;
  }
  return true;
}
inline bool write_all(HANDLE h, const void *p, uint32_t n) {
  auto *b = static_cast<const uint8_t *>(p);
  while (n) {
    DWORD k = 0;
    if (!WriteFile(h, b, n, &k, nullptr) || !k)
      return false;
    b += k;
    n -= k;
  }
  return true;
}
inline std::wstring quote(const std::wstring &s) {
  std::wstring r = L"\"";
  size_t n = 0;
  for (wchar_t c : s) {
    if (c == L'\\') {
      n++;
      continue;
    }
    r.append(c == L'\"' ? n * 2 + 1 : n, L'\\');
    n = 0;
    r += c;
  }
  r.append(n * 2, L'\\');
  return r + L'\"';
}
struct Process {
  Handle process, thread, job;
};
inline Process spawn(std::wstring command, const std::vector<HANDLE> &inherited,
                     HANDLE in = nullptr, HANDLE out = nullptr, HANDLE error = nullptr) {
  STARTUPINFOEXW startup{};
  startup.StartupInfo.cb = sizeof(startup);
  if (in) {
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = in;
    startup.StartupInfo.hStdOutput = out;
    startup.StartupInfo.hStdError = error;
  }
  SIZE_T length = 0;
  InitializeProcThreadAttributeList(nullptr, 1, 0, &length);
  std::vector<uint8_t> attributes(length);
  startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
  require(InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &length) != 0,
          "Process attribute initialization failed");
  bool ready =
      UpdateProcThreadAttribute(startup.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                const_cast<HANDLE *>(inherited.data()),
                                inherited.size() * sizeof(HANDLE), nullptr, nullptr) != 0;
  PROCESS_INFORMATION info{};
  BOOL created =
      ready ? CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE,
                             EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_SUSPENDED,
                             nullptr, nullptr, &startup.StartupInfo, &info)
            : FALSE;
  DeleteProcThreadAttributeList(startup.lpAttributeList);
  require(created != FALSE, "Native child process could not start");
  Process result;
  result.process.reset(info.hProcess);
  result.thread.reset(info.hThread);
  result.job.reset(CreateJobObjectW(nullptr, nullptr));
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  if (!result.job ||
      !SetInformationJobObject(result.job, JobObjectExtendedLimitInformation, &limits,
                               sizeof(limits)) ||
      !AssignProcessToJobObject(result.job, result.process)) {
    TerminateProcess(result.process, ERROR_ACCESS_DENIED);
    throw std::runtime_error("Child process lifetime could not be bounded");
  }
  ResumeThread(result.thread);
  return result;
}
struct CounterGuard {
  std::atomic<uint32_t> &counter;
  explicit CounterGuard(std::atomic<uint32_t> &c) : counter(c) { counter++; }
  ~CounterGuard() { counter--; }
};
class Timer {
  Handle timer{CreateWaitableTimerExW(nullptr, nullptr, 2, TIMER_ALL_ACCESS)};

public:
  Timer() {
    if (!timer)
      timer.reset(CreateWaitableTimerW(nullptr, FALSE, nullptr));
    require(static_cast<bool>(timer), "Native clock timer unavailable");
  }
  void wait(uint64_t deadline, HANDLE wake = nullptr) {
    auto now = ticks();
    if (deadline <= now)
      return;
    LARGE_INTEGER due;
    due.QuadPart =
        -static_cast<LONGLONG>(std::max<uint64_t>(1, (deadline - now) * 10000000 / frequency()));
    require(SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE) != 0,
            "Native clock scheduling failed");
    HANDLE handles[] = {timer, wake};
    WaitForMultipleObjects(wake ? 2 : 1, handles, FALSE, INFINITE);
  }
  void until(uint64_t deadline) { wait(deadline); }
};
inline std::vector<uint8_t> base64(const std::string &text) {
  std::vector<uint8_t> result;
  uint32_t bits = 0;
  unsigned count = 0;
  for (char c : text) {
    if (c == '=')
      break;
    int value = c >= 'A' && c <= 'Z'   ? c - 'A'
                : c >= 'a' && c <= 'z' ? c - 'a' + 26
                : c >= '0' && c <= '9' ? c - '0' + 52
                : c == '+'             ? 62
                : c == '/'             ? 63
                                       : -1;
    require(value >= 0, "Invalid pinned codec vector");
    bits = (bits << 6) | static_cast<unsigned>(value);
    count += 6;
    if (count >= 8) {
      count -= 8;
      result.push_back(static_cast<uint8_t>(bits >> count));
    }
  }
  return result;
}
} // namespace bh
