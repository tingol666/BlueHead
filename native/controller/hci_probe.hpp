#pragma once
#include "bootstrap_contract.hpp"
#include "catalog.hpp"
namespace bh {
class BootstrapCommands : public ControllerBootstrap {
protected:
  BootstrapIO io;
  uint16_t expected = 0;
  bool replied = false;
  std::vector<uint8_t> response;
  std::string failure;
  double deadline = seconds() + 60;
  std::vector<uint8_t> command(uint16_t opcode, const std::vector<uint8_t> &data = {}) {
    expected = opcode;
    replied = false;
    response.clear();
    io.write(opcode, data, false);
    double until = std::min(deadline, seconds() + 2);
    while (!replied) {
      if (!failure.empty())
        throw firmware::Error("hci_protocol", "", failure);
      if (seconds() >= until)
        throw firmware::Error("hci_timeout", "", "controller initialization command timed out");
      io.pump();
    }
    expected = 0;
    if (!failure.empty())
      throw firmware::Error("hci_protocol", "", failure);
    if (response.empty() || response[0] != 0)
      throw firmware::Error("hci_rejected", "", "controller rejected an initialization command");
    return response;
  }

public:
  explicit BootstrapCommands(BootstrapIO ports) : io(std::move(ports)) {}
  void event(const uint8_t *p, uint32_t n) override {
    if (n < 2 || p[1] != n - 2) {
      failure = "invalid initialization event length";
      return;
    }
    if (p[0] == 0x0e && n >= 6 && expected && le16(p + 3) == expected) {
      if (replied || !p[2]) {
        failure = "duplicate/invalid initialization acknowledgement";
        return;
      }
      response.assign(p + 5, p + n);
      replied = true;
    } else if (p[0] == 0x0f && n == 6 && expected && le16(p + 4) == expected && p[2])
      failure = "initialization command status rejected";
  }
};
struct ControllerVersion {
  unsigned hci = 0, revision = 0, manufacturer = 0, rom = 0;
  static ControllerVersion parse(const std::vector<uint8_t> &bytes) {
    if (bytes.size() != 9 || bytes[0])
      throw firmware::Error("hci_protocol", "", "invalid standard HCI version response");
    return {bytes[1], le16(bytes.data() + 2), le16(bytes.data() + 5), le16(bytes.data() + 7)};
  }
  json facts(uint16_t vid, uint16_t pid) const {
    return {{"usb", json::array({vid, pid})},
            {"hci", hci},
            {"revision", revision},
            {"manufacturer", manufacturer},
            {"rom", rom}};
  }
};
} // namespace bh
