/* Exercise the production single USB owner against the offline Intel model. */
#include "runtime_usb.hpp"
#include <iostream>
struct Target final : bh::HciTarget {
  uint32_t media_route(uint16_t, uint16_t) override { return 0; }
  void media_packet(uint16_t, uint16_t, uint32_t, const uint8_t *, uint32_t) override {}
  bool voice_packet(uint16_t, uint32_t, const uint8_t *, uint32_t) override { return false; }
  uint32_t voice_output(uint16_t &, uint8_t *, uint32_t) override { return 0; }
  uint32_t voice_byte_rate() const override { return 0; }
  void disconnected(uint16_t) override {}
  void transport_lost() noexcept override {}
};
int wmain(int count, wchar_t **values) {
  if (count != 2)
    return 2;
  Target target;
  bh::Module mock(bh::fs::path(values[1]) / L"libusb-1.0.dll");
  bh::json report;
  auto start = bh::seconds();
  wchar_t mode[100]{};
  GetEnvironmentVariableW(L"BLUEHEAD_USB_FAULT", mode, 100);
  bool realtek = std::wstring(mode).rfind(L"realtek-", 0) == 0;
  bool standard = std::wstring(mode).rfind(L"standard-", 0) == 0;
  uint16_t vid = realtek ? 0x0bda : standard ? 0x1234 : 0x8087;
  uint16_t pid = realtek ? (std::wstring(mode) == L"realtek-unknown-board" ? 1 : 0x8771) : standard ? 0xabcd : 0x0033;
  try {
    bh::Usb usb(values[1], target, std::wstring(mode) == L"intel-ambiguous");
    auto before = bh::seconds();
    usb.start(vid, pid, 0);
    report["start_milliseconds"] = (bh::seconds() - before) * 1000;
    while (usb.startup()["starting"] == true && bh::seconds() - start < 10) {
      if (std::wstring(mode) == L"intel-cancel" &&
          mock.get<int (*)()>("fake_upload_commands")() > 0)
        break;
      Sleep(1);
    }
    report["startup"] = usb.startup();
    report["active"] = usb.active();
    usb.close();
    report["closed"] = !usb.active();
    if (report["active"] == true) {
      bh::Usb next(values[1], target);
      next.open(vid, pid, 0);
      report["reopened"] = next.startup();
      next.close();
    }
  } catch (const std::exception &error) {
    report["error"] = error.what();
  }
  for (const char *name : {"fake_bad_release", "fake_pending", "fake_upload_commands",
                           "fake_bulk_violations", "fake_firmware_loads"})
    report[name] = mock.get<int (*)()>(name)();
  report["seconds"] = bh::seconds() - start;
  std::cout << report.dump() << '\n';
  return 0;
}
