/* Two identical offline adapters, reordered enumeration and independent leases. */
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
  auto resolver = [](unsigned long n) {
    return n == 1 ? "USB\\VID_1234&PID_ABCD\\SAMPLE_A" : "USB\\VID_1234&PID_ABCD\\SAMPLE_B";
  };
  bh::json report;
  bh::Module mock(bh::fs::path(values[1]) / L"libusb-1.0.dll");
  wchar_t fault[64]{};
  GetEnvironmentVariableW(L"BLUEHEAD_USB_FAULT", fault, 64);
  try {
    bh::Usb usb(values[1], target, false, resolver);
    if (std::wstring(fault) == L"identity-legacy")
      usb.start(0x1234, 0xabcd, 0);
    else
      usb.start_id(bh::physical_id(resolver(1)));
    while (usb.startup()["starting"] == true)
      Sleep(1);
    report["startup"] = usb.startup();
    report["active"] = usb.active();
    report["selected_device"] = mock.get<int (*)()>("fake_opened_device")();
    if (usb.active()) {
      bh::Usb duplicate(values[1], target, false, resolver);
      duplicate.start_id(bh::physical_id(resolver(1)));
      while (duplicate.startup()["starting"] == true)
        Sleep(1);
      report["duplicate"] = duplicate.startup();
      report["duplicate_active"] = duplicate.active();
      duplicate.close();
      report["original_still_active"] = usb.active();
    }
    usb.close();
    report["canonical_case"] =
        bh::physical_id("usb\\vid_1234&pid_abcd\\sample_a") == bh::physical_id(resolver(1));
    report["distinct_instances"] = bh::physical_id(resolver(1)) != bh::physical_id(resolver(2));
  } catch (const std::exception &error) {
    report["error"] = error.what();
  }
  report["invalid_releases"] = mock.get<int (*)()>("fake_bad_release")();
  report["pending"] = mock.get<int (*)()>("fake_pending")();
  std::cout << report.dump() << '\n';
  return 0;
}
