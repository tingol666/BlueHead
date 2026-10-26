#pragma once
#include "rpc_server.hpp"
#include <deque>
#include <fstream>
#include <iostream>
namespace bh {
inline std::wstring pipe_name() {
  std::array<uint8_t, 16> nonce{};
  require(BCryptGenRandom(nullptr, nonce.data(), nonce.size(), BCRYPT_USE_SYSTEM_PREFERRED_RNG) >=
              0,
          "Cannot create private channel identity");
  return L"\\\\.\\pipe\\BlueHead-RT-" + wide(hex(nonce.data(), nonce.size()));
}
struct Arguments {
  fs::path assets, data, source;
  std::wstring rpc;
  bool engine = false, offline = false;
  DWORD parent = 0;
  HANDLE mapping = nullptr, request = nullptr, done = nullptr;
  explicit Arguments(int count, wchar_t **values) {
    for (int i = 1; i < count; i++) {
      std::wstring key = values[i];
      auto value = [&]() {
        require(i + 1 < count, "Missing native argument");
        return std::wstring(values[++i]);
      };
      if (key == L"--assets")
        assets = value();
      else if (key == L"--rpc")
        rpc = value();
      else if (key == L"--engine")
        engine = true;
      else if (key == L"--offline-test")
        offline = true;
      else if (key == L"--data-dir")
        data = value();
      else if (key == L"--source-root")
        source = value();
      else if (key == L"--parent-pid")
        parent = std::stoul(value());
      else if (key == L"--ldac-worker") {
        mapping = reinterpret_cast<HANDLE>(std::stoull(value()));
        request = reinterpret_cast<HANDLE>(std::stoull(value()));
        done = reinterpret_cast<HANDLE>(std::stoull(value()));
      } else
        throw std::runtime_error("Unknown native argument");
    }
    if (assets.empty()) {
      auto here = executable().parent_path();
      assets =
          fs::exists(here / L"bluehead_audio.dll") ? here : here / L"_internal/bluehead/assets";
    }
    assets = fs::absolute(assets);
    if (source.empty()) {
      wchar_t buffer[32768];
      DWORD n = GetEnvironmentVariableW(L"BLUEHEAD_SOURCE_ROOT", buffer, 32768);
      if (n && n < 32768)
        source = buffer;
    }
  }
};
class Host {
  RpcServer &rpc;
  Arguments args;
  Process child;
  Handle input, output, error;
  std::thread input_writer, output_reader, error_reader;
  std::mutex queue_mutex, stdout_mutex;
  std::condition_variable condition;
  std::deque<std::string> queue;
  std::atomic<bool> stopping{false};
  std::atomic<bool> control_failed{false};
  std::atomic<uint64_t> stop_at{0};
  uint64_t parameter_revision = 0;
  void emit_line(const std::string &text) {
    std::lock_guard<std::mutex> lock(stdout_mutex);
    write_all(GetStdHandle(STD_OUTPUT_HANDLE), text.data(), static_cast<uint32_t>(text.size()));
  }
  static std::string coalesce_key(const json &j) {
    auto name = j.value("command", std::string());
    if (name == "master_gain")
      return name + ":" + j.value("bus", std::string());
    if (name == "mute")
      return name;
    if (name == "gain")
      return name + ":" + j.value("key", std::string()) + ":" +
             j.value("direction", std::string("down"));
    return {};
  }
  void enqueue(const std::string &line) {
    auto request = json::parse(line);
    auto key = coalesce_key(request), name = request.value("command", std::string());
    std::lock_guard<std::mutex> lock(queue_mutex);
    if (name == "stop" || name == "shutdown") {
      // Native quiesce has already stopped sound. Keep the latest volume/mute
      // intent so a fast exit cannot discard settings before Python saves them.
      // Other queued actions must not restart or reopen a stopped receiver.
      std::deque<std::string> retained;
      std::vector<std::string> seen;
      for (auto it = queue.rbegin(); it != queue.rend(); ++it) {
        auto parameter = coalesce_key(json::parse(*it));
        if (parameter.empty() || std::find(seen.begin(), seen.end(), parameter) != seen.end())
          continue;
        seen.push_back(parameter);
        retained.push_front(*it);
      }
      queue.swap(retained);
    }
    if (!key.empty()) {
      for (auto it = queue.rbegin(); it != queue.rend(); ++it) {
        auto pending = json::parse(*it);
        if (coalesce_key(pending) == key) {
          *it = line;
          condition.notify_one();
          return;
        }
        // Never reorder a gain across start/stop or a settings transaction.
        if (coalesce_key(pending).empty())
          break;
      }
    }
    require(queue.size() < 128, "Bluetooth control queue is full");
    queue.push_back(line);
    condition.notify_one();
  }
  void critical(const json &request) {
    auto command = request.value("command", std::string());
    if (command == "master_gain" && request.contains("gain") && request["gain"].is_number())
      rpc.execute({{"op", "master_gain"},
                   {"bus", request.value("bus", std::string())},
                   {"gain", request["gain"]},
                   {"revision", ++parameter_revision}});
    if (command == "mute" && request.contains("muted") && request["muted"].is_boolean())
      rpc.execute(
          {{"op", "mute"}, {"muted", request["muted"]}, {"revision", ++parameter_revision}});
    if (command == "stop" || command == "shutdown") {
      stop_at = ticks() + frequency() * 5;
      rpc.execute({{"op", "quiesce"}});
    }
  }
  static bool line(HANDLE h, std::string &out) {
    out.clear();
    char c;
    while (out.size() < 262144) {
      DWORD got = 0;
      if (!ReadFile(h, &c, 1, &got, nullptr) || !got)
        return false;
      if (c == '\n')
        return true;
      out += c;
    }
    throw std::runtime_error("Control line exceeds limit");
  }

public:
  Host(RpcServer &server, const Arguments &arguments) : rpc(server), args(arguments) {
    try {
      SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
      Handle child_in, child_out, child_error;
      auto make_pipe = [&](Handle &read, Handle &write, DWORD size) {
        HANDLE raw_read = nullptr, raw_write = nullptr;
        require(CreatePipe(&raw_read, &raw_write, &security, size),
                "Cannot create control process pipes");
        read.reset(raw_read);
        write.reset(raw_write);
      };
      make_pipe(child_in, input, 65536);
      make_pipe(output, child_out, 65536);
      make_pipe(error, child_error, 8192);
      require(SetHandleInformation(input, HANDLE_FLAG_INHERIT, 0) &&
                  SetHandleInformation(output, HANDLE_FLAG_INHERIT, 0) &&
                  SetHandleInformation(error, HANDLE_FLAG_INHERIT, 0),
              "Cannot restrict control pipe inheritance");
      std::wstring command;
      auto packaged = executable().parent_path() / L"BlueHead.Engine.exe";
      if (fs::exists(packaged))
        command = quote(packaged.wstring());
      else {
        require(!args.source.empty(), "Source root is required for development native host");
        command = quote((args.source / L".venv/Scripts/python.exe").wstring()) + L" -X utf8 " +
                  quote((args.source / L"launch.py").wstring());
      }
      command += L" --engine --data-dir " + quote(args.data.wstring());
      SetEnvironmentVariableW(L"BLUEHEAD_NATIVE_PIPE", args.rpc.c_str());
      SetEnvironmentVariableW(L"BLUEHEAD_NATIVE_ASSETS", args.assets.c_str());
      child = spawn(command, {child_in, child_out, child_error}, child_in, child_out, child_error);
      input_writer = std::thread([this] {
        while (true) {
          std::string message;
          {
            std::unique_lock<std::mutex> lock(queue_mutex);
            condition.wait(lock, [this] { return stopping || !queue.empty(); });
            if (stopping && queue.empty())
              break;
            message = std::move(queue.front());
            queue.pop_front();
          }
          message += '\n';
          if (!write_all(input, message.data(), static_cast<uint32_t>(message.size()))) {
            control_failed = true;
            break;
          }
        }
      });
      output_reader = std::thread([this] {
        try {
          std::string message;
          while (line(output, message)) {
            auto j = json::parse(message);
            if (j.value("event", std::string()) == "stopped")
              stop_at = 0;
            message += '\n';
            emit_line(message);
          }
        } catch (const std::exception &) {
          control_failed = true;
        };
        control_failed = true;
      });
      error_reader = std::thread([this] {
        std::array<uint8_t, 4096> b{};
        DWORD n = 0;
        while (ReadFile(error, b.data(), b.size(), &n, nullptr) && n)
          write_all(GetStdHandle(STD_ERROR_HANDLE), b.data(), n);
      });
    } catch (...) {
      stop_and_join();
      throw;
    }
  }
  ~Host() { stop_and_join(); }

private:
  void stop_and_join() {
    stopping = true;
    condition.notify_all();
    if (child.process && WaitForSingleObject(child.process, 2000) != WAIT_OBJECT_0)
      child.job.reset();
    // Do not close a HANDLE while another thread may be using it. Killing
    // the child closes its read end and retires a blocked WriteFile first.
    if (input_writer.joinable())
      input_writer.join();
    input.reset();
    if (output_reader.joinable())
      output_reader.join();
    if (error_reader.joinable())
      error_reader.join();
  }

public:
  int run() {
    std::atomic<bool> finished{false};
    Handle stdin_thread;
    HANDLE duplicate = nullptr;
    DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &duplicate,
                    THREAD_TERMINATE | THREAD_QUERY_INFORMATION, FALSE, 0);
    stdin_thread.reset(duplicate);
    std::thread monitor([this, &finished, &stdin_thread] {
      while (!finished) {
        if (WaitForSingleObject(child.process, 100) == WAIT_OBJECT_0 || rpc.exited() ||
            control_failed) {
          if (stdin_thread)
            CancelSynchronousIo(stdin_thread);
          break;
        }
        if (stop_at && ticks() > stop_at) {
          child.job.reset();
          // Covers a stuck native OS operation too. Closing this process also
          // closes its kill-on-close child job; no stale USB/audio owner survives.
          TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
        }
      }
    });
    try {
      std::string message;
      while (line(GetStdHandle(STD_INPUT_HANDLE), message)) {
        auto request = json::parse(message);
        require(request.is_object() && request.value("protocol", 0) == 1,
                "Control protocol mismatch");
        critical(request);
        enqueue(message);
        if (request.value("command", std::string()) == "shutdown")
          break;
      }
    } catch (const std::exception &e) {
      auto report = json{{"event", "error"}, {"protocol", 1}, {"message", e.what()}}.dump() + "\n";
      emit_line(report);
    }
    finished = true;
    monitor.join();
    return 0;
  }
};
} // namespace bh
