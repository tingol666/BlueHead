#pragma once
#include "runtime_core.hpp"
#include <sddl.h>
namespace bh {
class RpcServer {
  Core &core;
  fs::path assets;
  std::wstring name;
  Handle pipe, thread_handle;
  Handle thread_ready{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
  std::thread worker;
  std::atomic<bool> stopping{false}, connected{false};
  std::mutex commands;

public:
  RpcServer(Core &c, const fs::path &p, const std::wstring &n) : core(c), assets(p), name(n) {
    Handle token;
    HANDLE raw = nullptr;
    require(OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw) != 0,
            "Cannot secure native control channel");
    token.reset(raw);
    DWORD length = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &length);
    std::vector<uint8_t> user(length);
    require(GetTokenInformation(token, TokenUser, user.data(), length, &length) != 0,
            "Cannot secure native control channel");
    wchar_t *sid = nullptr;
    require(ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER *>(user.data())->User.Sid, &sid) !=
                0,
            "Cannot identify control owner");
    std::wstring sddl = L"D:P(A;;GA;;;SY)(A;;GA;;;" + std::wstring(sid) + L")";
    LocalFree(sid);
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    require(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1,
                                                                 &descriptor, nullptr) != 0,
            "Cannot secure native pipe ACL");
    SECURITY_ATTRIBUTES security{sizeof(security), descriptor, FALSE};
    pipe.reset(CreateNamedPipeW(name.c_str(), PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                                PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1, 131072,
                                131072, 0, &security));
    LocalFree(descriptor);
    require(static_cast<bool>(pipe), "Cannot create private native control pipe");
    require(static_cast<bool>(thread_ready), "Cannot create control thread event");
    worker = std::thread([this] { run(); });
    if (WaitForSingleObject(thread_ready, 5000) != WAIT_OBJECT_0) {
      stop();
      throw std::runtime_error("Native control thread did not start");
    }
  }
  ~RpcServer() { stop(); }
  json execute(const json &request) {
    auto op = request.value("op", std::string());
    if (op == "master_gain" || op == "mute")
      return core.command(request, assets);
    if (op == "quiesce")
      core.quiesce_now();
    std::lock_guard<std::mutex> lock(commands);
    return core.command(request, assets);
  }
  bool exited() const { return stopping.load(); }
  void stop() {
    stopping = true;
    if (pipe)
      CancelIoEx(pipe, nullptr);
    if (thread_handle)
      CancelSynchronousIo(thread_handle);
    if (pipe)
      DisconnectNamedPipe(pipe);
    if (worker.joinable())
      worker.join();
    pipe.reset();
  }
  void run() {
    HANDLE duplicate = nullptr;
    DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &duplicate,
                    THREAD_TERMINATE | THREAD_QUERY_INFORMATION, FALSE, 0);
    thread_handle.reset(duplicate);
    SetEvent(thread_ready);
    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
      stopping = true;
      return;
    }
    connected = true;
    while (!stopping) {
      uint32_t length = 0;
      if (!read_all(pipe, &length, 4) || !length || length > 131072)
        break;
      std::string text(length, 0);
      if (!read_all(pipe, text.data(), length))
        break;
      json reply;
      try {
        auto request = json::parse(text);
        require(request.is_object(), "Invalid native request");
        if (request.value("op", std::string()) == "runtime_exit") {
          reply = {{"ok", true}, {"result", true}};
          stopping = true;
        } else
          reply = {{"ok", true}, {"result", execute(request)}};
      } catch (const SourceExpired &e) {
        reply = {{"ok", false}, {"error", e.what()}, {"category", "source_expired"}};
      } catch (const AudioFailure &e) {
        reply = {{"ok", false},
                 {"error", e.what()},
                 {"category", fault_name(e.category)},
                 {"native_code", e.code},
                 {"retryable", false}};
      } catch (const std::exception &e) {
        reply = {{"ok", false}, {"error", e.what()}};
      }
      auto encoded = reply.dump();
      length = static_cast<uint32_t>(encoded.size());
      if (length > 1048576 || !write_all(pipe, &length, 4) ||
          !write_all(pipe, encoded.data(), length))
        break;
    }
    connected = false;
    stopping = true;
  }
};
} // namespace bh
