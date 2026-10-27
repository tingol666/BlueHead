#pragma once
#include "hci_probe.hpp"
#include "intel_firmware.hpp"
#include "realtek_bootstrap.hpp"
namespace bh {
class ControllerInitializer final : public BootstrapCommands {
  std::unique_ptr<ControllerBootstrap> vendor;
  uint16_t vid, pid;
  std::string backend = "standard-hci";
  json evidence = json::object();

public:
  ControllerInitializer(uint16_t v, uint16_t p, BootstrapIO ports)
      : BootstrapCommands(std::move(ports)), vid(v), pid(p) {
    if (IntelFirmware::supported(vid, pid)) {
      vendor = std::make_unique<IntelFirmware>(io.write, io.pump, io.progress);
      backend = "intel-tlv";
      bulk_events = true;
    }
  }
  const char *name() const override { return backend.c_str(); }
  void event(const uint8_t *p, uint32_t n) override {
    if (vendor)
      vendor->event(p, n);
    else
      BootstrapCommands::event(p, n);
  }
  void run(const fs::path &assets) override {
    bool initialized = bool(vendor);
    if (vendor) {
      vendor->run(assets);
      vendor.reset();
      bulk_events = false;
    }
    if (!initialized)
      io.progress("probing", 100);
    if (!initialized)
      command(0x0c03);
    auto version = ControllerVersion::parse(command(0x1001));
    if (backend == "standard-hci") {
      switch (version.manufacturer) {
      case 2:
        backend = "intel-operational";
        break;
      case 15:
        backend = "broadcom-operational";
        break;
      case 29:
        backend = "qualcomm-operational";
        break;
      case 70:
        backend = "mediatek-operational";
        break;
      case 93:
        backend = "realtek-operational";
        break;
      default:
        break;
      }
    }
    if (version.manufacturer == 93 &&
        (version.rom == 0x8723 || version.rom == 0x8761 || version.rom == 0x8821 ||
         version.rom == 0x8822 || version.rom == 0x8852)) {
      backend = "realtek";
      vendor = std::make_unique<RealtekBootstrap>(io, version, vid, pid);
      vendor->run(assets);
      vendor.reset();
      initialized = true;
      version = ControllerVersion::parse(command(0x1001));
    }
    auto features = command(0x1003), buffers = command(0x1005), address = command(0x1009);
    if (features.size() != 9 || buffers.size() != 8 || address.size() != 7)
      throw firmware::Error("hci_protocol", "", "malformed HCI capability response");
    bool bredr = !(features[5] & 0x20);
    bool sco = (features[2] & 0x08) || (features[4] & 0x80);
    if (!bredr)
      throw firmware::Error("unsupported_capability", "",
                            "classic Bluetooth BR/EDR is unavailable");
    if (!le16(buffers.data() + 1) || !le16(buffers.data() + 4))
      throw firmware::Error("hci_not_initialized", "",
                            "controller ACL buffers are not initialized");
    bool nonzero = false, nonff = false;
    for (size_t i = 1; i < 7; ++i) {
      nonzero |= address[i] != 0;
      nonff |= address[i] != 0xff;
    }
    const std::array<uint8_t, 6> placeholder{0x00, 0x8b, 0x9e, 0x19, 0x03, 0x00};
    if (!nonzero || !nonff ||
        std::equal(placeholder.begin(), placeholder.end(), address.begin() + 1))
      throw firmware::Error("hci_not_initialized", "", "controller address is not initialized");
    evidence = {{"ready", true},
                {"media_capable", true},
                {"voice_capable", sco},
                {"hci_version", version.hci},
                {"manufacturer", version.manufacturer},
                {"acl_packet_bytes", le16(buffers.data() + 1)},
                {"acl_packets", le16(buffers.data() + 4)},
                {"features", hex(features.data() + 1, 8)},
                {"backend", backend}};
    if (!initialized)
      io.progress("ready", 100);
  }
  json result() const override { return evidence; }
};
} // namespace bh
