#pragma once
#include "hci_probe.hpp"
#include "intel_firmware.hpp"
#include "realtek_bootstrap.hpp"
#include "address_profile.hpp"
namespace bh {
class ControllerInitializer final : public BootstrapCommands {
  std::unique_ptr<ControllerBootstrap> vendor;
  uint16_t vid, pid;
  std::string backend = "standard-hci";
  json evidence = json::object();
  ControllerAddressProfile address_profile;
  bool restore_original = false;

public:
  ControllerInitializer(uint16_t v, uint16_t p, BootstrapIO ports,
                        ControllerAddressProfile profile = {}, bool restore_address = false)
      : BootstrapCommands(std::move(ports)), vid(v), pid(p),
        address_profile(std::move(profile)), restore_original(restore_address) {
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
    json identity = {{"configured", address_profile.valid()}, {"separated", false},
                     {"reason", address_profile.valid() ? "unsupported" : "not_prepared"}};
    if (address_profile.valid()) {
      auto actual = std::vector<uint8_t>(address.begin() + 1, address.end());
      const auto &wanted = restore_original ? address_profile.original : address_profile.dedicated;
      if (actual != address_profile.original && actual != address_profile.dedicated)
        throw firmware::Error("identity_mismatch", "", "controller identity differs from its saved adapter profile");
      auto opcode = ControllerAddressProfile::write_opcode(version.manufacturer);
      if (actual == wanted) identity["reason"] = "verified";
      else if (opcode) {
        try {
          command(opcode, wanted);
          address = command(0x1009);
          if (address.size() != 7 || !std::equal(wanted.begin(), wanted.end(), address.begin() + 1))
            throw firmware::Error("identity_not_applied", "", "controller did not apply its runtime address");
          identity["reason"] = "verified";
        } catch (const firmware::Error &error) {
          if (error.code != "hci_rejected") throw;
          // Rejecting an unsupported command must leave the previous identity
          // unchanged. Never report successful separation from a software alias.
          address = command(0x1009);
          if (address.size() != 7 || !std::equal(actual.begin(), actual.end(), address.begin() + 1))
            throw firmware::Error("identity_restore_failed", "", "controller identity could not be verified after rejection");
          identity["reason"] = "rejected";
        }
      }
      identity["separated"] = address.size() == 7 &&
          std::equal(address_profile.dedicated.begin(), address_profile.dedicated.end(), address.begin() + 1);
      if (restore_original && identity["separated"] == true)
        throw firmware::Error("identity_restore_failed", "", "original controller address has not been restored");
    }
    evidence = {{"ready", true},
                {"media_capable", true},
                {"voice_capable", sco},
                {"hci_version", version.hci},
                {"manufacturer", version.manufacturer},
                {"acl_packet_bytes", le16(buffers.data() + 1)},
                {"acl_packets", le16(buffers.data() + 4)},
                {"sco_packet_bytes", buffers[3]},
                {"sco_packets", le16(buffers.data() + 6)},
                {"features", hex(features.data() + 1, 8)},
                {"backend", backend}, {"identity", identity}};
    if (restore_original) {
      // Only the explicit administrative preparation tool receives this field.
      // Normal receiver telemetry contains no local Bluetooth address.
      auto printed = std::vector<uint8_t>(address.begin() + 1, address.end());
      std::reverse(printed.begin(), printed.end());
      evidence["address"] = hex(printed.data(), printed.size());
    }
    if (!initialized)
      io.progress("ready", 100);
  }
  json result() const override { return evidence; }
};
} // namespace bh
