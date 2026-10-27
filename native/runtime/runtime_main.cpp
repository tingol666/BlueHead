/* BlueHead native application entry point. Apache-2.0. */
#include "application_host.hpp"
#include <tlhelp32.h>
namespace {
bh::Handle driver_parent(DWORD wanted) {
  bh::require(wanted != 0, "Driver firmware preparation requires its driver helper parent");
  bh::Handle snapshot(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0));
  bh::require(bool(snapshot), "Cannot verify driver helper parent");
  PROCESSENTRY32W entry{};
  entry.dwSize = sizeof(entry);
  DWORD actual = 0;
  if (Process32FirstW(snapshot, &entry))
    do {
      if (entry.th32ProcessID == GetCurrentProcessId()) {
        actual = entry.th32ParentProcessID;
        break;
      }
    } while (Process32NextW(snapshot, &entry));
  bh::require(actual == wanted, "Driver firmware preparation parent mismatch");
  bh::Handle parent(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, wanted));
  wchar_t path[32768];
  DWORD size = 32768;
  bh::require(
      parent && QueryFullProcessImageNameW(parent, 0, path, &size) &&
          bh::fs::equivalent(bh::fs::path(path), bh::executable().parent_path().parent_path() /
                                                     L"BlueHead.DriverHelper.exe"),
      "Driver firmware preparation requires the installed driver helper");
  bh::Handle operation(OpenMutexW(SYNCHRONIZE, FALSE, L"Global\\BlueHead.DriverOperation"));
  bh::require(bool(operation), "No active driver operation for firmware preparation");
  return parent;
}
struct BootstrapTarget final : bh::HciTarget {
  uint32_t media_route(uint16_t, uint16_t) override { return 0; }
  void media_packet(uint16_t, uint16_t, uint32_t, const uint8_t *, uint32_t) override {}
  bool voice_packet(uint16_t, uint32_t, const uint8_t *, uint32_t) override { return false; }
  uint32_t voice_output(uint16_t &, uint8_t *, uint32_t) override { return 0; }
  uint32_t voice_byte_rate() const override { return 0; }
  void disconnected(uint16_t) override {}
  void transport_lost() noexcept override {}
};
} // namespace
int wmain(int count, wchar_t **values) {
  try {
    bh::Arguments args(count, values);
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    AddDllDirectory(args.assets.c_str());
    AddDllDirectory(args.assets.parent_path().parent_path().c_str());
    if (args.prepare_controller) {
      bh::require(!args.offline && !args.engine && args.rpc.empty() && !args.mapping,
                  "Driver firmware preparation is unavailable in offline/RPC/engine modes");
      auto parent = driver_parent(args.parent);
      BootstrapTarget target;
      // This recovery-only command refuses ambiguous physical devices. It
      // releases every transfer and HCI lease BEFORE a driver can be rebound.
      bh::Usb usb(args.assets, target, true);
      bh::require(bh::valid_adapter_id(args.adapter_id), "Driver firmware preparation requires a physical adapter identity");
      usb.start_id(args.adapter_id);
      auto deadline = bh::seconds() + 65;
      while (usb.startup()["starting"] == true) {
        bh::require(WaitForSingleObject(parent, 0) != WAIT_OBJECT_0,
                    "Driver helper exited during firmware preparation");
        bh::require(bh::seconds() < deadline, "Driver firmware preparation timed out");
        Sleep(20);
      }
      auto status = usb.startup();
      if (!usb.active())
        throw std::runtime_error(status.value("error", "Controller initialization failed"));
      bh::require(status["firmware_state"] == "loaded" || status["firmware_state"] == "already_loaded",
                  "Controller native preparation is unavailable; use original-driver initialization");
      usb.close();
      std::cout << status.dump() << '\n';
      return 0;
    }
    if (args.mapping)
      return bh::ldac_worker(args.mapping, args.request, args.done, args.assets);
    if (args.rpc.empty())
      args.rpc = bh::pipe_name();
    bh::Core core(args.assets, args.offline);
    bh::RpcServer server(core, args.assets, args.rpc);
    if (args.engine) {
      bh::Host host(server, args);
      return host.run();
    }
    bh::Handle parent(args.parent ? OpenProcess(SYNCHRONIZE, FALSE, args.parent) : nullptr);
    while (!server.exited()) {
      if (parent && WaitForSingleObject(parent, 100) == WAIT_OBJECT_0)
        break;
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "Native runtime: " << e.what() << '\n';
    return 2;
  }
}
