/* Per-adapter identities. Runtime address commands only; never EEPROM/flash.
 * Protocol command numbers are independently implemented from public Linux
 * btintel_set_bdaddr/btbcm_set_bdaddr. No kernel source is incorporated.
 * SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include "controller_identity.hpp"
#include <fstream>
#include <cctype>
namespace bh {
struct ControllerAddressProfile {
  std::vector<uint8_t> original, dedicated;
  static std::vector<uint8_t> parse(const std::string &text) {
    if (text.size() != 12) return {};
    for (char c : text)
      if (!std::isxdigit(static_cast<unsigned char>(c))) return {};
    auto bytes = unhex(text, 6);
    if (bytes.size() != 6 || (bytes[0] & 1) ||
        std::all_of(bytes.begin(), bytes.end(), [](uint8_t b) { return b == 0; }) ||
        std::all_of(bytes.begin(), bytes.end(), [](uint8_t b) { return b == 0xff; }))
      return {};
    std::reverse(bytes.begin(), bytes.end()); // HCI BD_ADDR uses little endian.
    return bytes;
  }
  bool valid() const { return original.size() == 6 && dedicated.size() == 6 && original != dedicated; }
  static ControllerAddressProfile load(const std::string &adapter) {
    if (!valid_adapter_id(adapter)) return {};
    wchar_t common[32768]{};
    auto size = GetEnvironmentVariableW(L"ProgramData", common, 32768);
    if (!size || size >= 32768) return {};
    auto key = adapter;
    std::transform(key.begin(), key.end(), key.begin(), [](char c) { return char(std::toupper(static_cast<unsigned char>(c))); });
    auto folder = fs::path(common) / L"BlueHead" / L"Drivers";
    auto path = folder / L"Identities" / wide(key + ".json");
    try {
      if (!fs::exists(path)) path = folder / wide(key) / L"recovery.json";
      if (!fs::is_regular_file(path) || fs::file_size(path) > 65536) return {};
      std::ifstream file(path, std::ios::binary);
      auto record = json::parse(file);
      auto instance = canonical_instance(record.value("InstanceId", std::string()));
      if (identity_hash(instance) != adapter) return {};
      ControllerAddressProfile profile{parse(record.value("OriginalAddress", std::string())),
                                       parse(record.value("DedicatedAddress", std::string()))};
      return profile.valid() ? profile : ControllerAddressProfile{};
    } catch (const std::exception &) { return {}; }
  }
  static uint16_t write_opcode(unsigned manufacturer) {
    switch (manufacturer) {
    case 2: return 0xfc31; // Intel operational firmware
    case 15: return 0xfc01; // Broadcom/Cypress operational firmware
    default: return 0;
    }
  }
};
} // namespace bh
