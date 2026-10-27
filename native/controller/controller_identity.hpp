/* Physical adapter identity and bounded capability records. Apache-2.0.
 * Windows libusb 1.0.30 exposes DEVINST through libusb_get_session_data.
 * That backend contract avoids cross-enumerator index matching. */
#pragma once
#include "runtime_common.hpp"
#include <bcrypt.h>
namespace bh {
inline std::string identity_hash(const std::string &text) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD count = 0, object_size = 0;
  require(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0,
          "Controller identity hash unavailable");
  BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&object_size),
                    sizeof(object_size), &count, 0);
  std::vector<uint8_t> object(object_size);
  std::array<uint8_t, 32> digest{};
  auto a = BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0);
  auto b = a >= 0 ? BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<char *>(text.data())),
                                   static_cast<ULONG>(text.size()), 0)
                  : a;
  auto c = b >= 0 ? BCryptFinishHash(hash, digest.data(), 32, 0) : b;
  if (hash)
    BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  require(c >= 0, "Controller identity hash failed");
  return hex(digest.data(), 12);
}
inline bool valid_adapter_id(const std::string &id) {
  return id.size() == 24 && std::all_of(id.begin(), id.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
inline std::string canonical_instance(std::string id) {
  require(id.size() >= 5 && id.size() <= 512, "Invalid physical controller identity");
  for (char &c : id) {
    require(static_cast<unsigned char>(c) >= 32 && static_cast<unsigned char>(c) < 127,
            "Invalid physical controller identity character");
    if (c >= 'a' && c <= 'z')
      c = char(c - 'a' + 'A');
  }
  require(id.rfind("USB\\", 0) == 0, "Physical controller must be a USB device");
  return id;
}
inline std::string physical_id(const std::string &instance) {
  return identity_hash(canonical_instance(instance));
}
inline std::string windows_instance(unsigned long node) {
  if (!node)
    return {}; // Offline USB test backend; not a publishable adapter.
  using GetId = unsigned long(WINAPI *)(unsigned long, wchar_t *, unsigned long, unsigned long);
  using GetParent = unsigned long(WINAPI *)(unsigned long *, unsigned long, unsigned long);
  wchar_t system[MAX_PATH]{};
  require(GetSystemDirectoryW(system, MAX_PATH) > 0, "Windows device APIs unavailable");
  Module module(fs::path(system) / L"cfgmgr32.dll");
  auto get_id = module.get<GetId>("CM_Get_Device_IDW");
  auto parent = module.get<GetParent>("CM_Get_Parent");
  std::string result;
  for (int depth = 0; get_id && parent && depth < 8; ++depth) {
    wchar_t buffer[1024]{};
    if (get_id(node, buffer, 1024, 0) != 0)
      break;
    auto id = utf8(buffer);
    if (id.rfind("USB\\VID_", 0) == 0 && id.find("&MI_") == std::string::npos) {
      result = canonical_instance(id);
      break;
    }
    unsigned long next = 0;
    if (parent(&next, node, 0) != 0)
      break;
    node = next;
  }
  return result;
}
struct ControllerCapabilities {
  bool media_transport = false, voice_transport = false;
  std::string backend = "generic", firmware = "not_checked";
  json hci = json::object();
  json snapshot() const {
    return {{"media_transport", media_transport},
            {"voice_transport", voice_transport},
            {"backend", backend},
            {"firmware", firmware},
            {"hci", hci},
            {"voice_verified", false},
            {"scope", "descriptor_and_initialization"}};
  }
};
} // namespace bh
