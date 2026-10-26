/* Execute the unmodified production USB owner against an offline USB DLL. */
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
  try {
    bh::Usb usb(values[1], target);
    usb.open(0x1234, 0xabcd, 0);
    report["opened"] = usb.active();
    wchar_t fault[64]{};
    GetEnvironmentVariableW(L"BLUEHEAD_USB_FAULT", fault, 64);
    if (std::wstring(fault) == L"control-timeout") {
      const uint8_t reset[] = {1, 3, 12, 0};
      bh::require(usb.send(reset, sizeof(reset)), "Could not send fake Reset");
      while (usb.active() && bh::seconds() - start < 4)
        Sleep(5);
      report["terminal"] = usb.snapshot();
      bh::require(!usb.active(), "HCI OUT remained pending without a deadline");
    }
    usb.close();
    report["closed"] = !usb.active();
    // Releasing the native HCI lease must permit another owner immediately.
    bh::Usb next(values[1], target);
    next.open(0x1234, 0xabcd, 0);
    next.close();
    report["reopened"] = true;
  } catch (const std::exception &error) {
    report["error"] = error.what();
  }
  report["invalid_releases"] = mock.get<int (*)()>("fake_bad_release")();
  report["pending_after_close"] = mock.get<int (*)()>("fake_pending")();
  report["seconds"] = bh::seconds() - start;
  std::cout << report.dump() << '\n';
  return 0;
}
