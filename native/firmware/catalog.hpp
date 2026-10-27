/* Signed firmware metadata and bounded content store. Apache-2.0.
 * Neither the catalog nor acquisition code owns or opens USB/HCI. */
#pragma once
#include "runtime_common.hpp"
#include "catalog_trust.hpp"
#include <bcrypt.h>
#include <set>
#include <fstream>
namespace bh::firmware {
inline constexpr size_t max_artifact = 8 * 1024 * 1024;
struct Error : std::runtime_error {
  std::string code, profile;
  Error(std::string kind, std::string id, const std::string &detail)
      : std::runtime_error("Firmware: " + detail), code(std::move(kind)), profile(std::move(id)) {}
};
inline void check(bool valid, const char *message) {
  if (!valid)
    throw Error("invalid_firmware", "", message);
}
inline std::string digest(const std::vector<uint8_t> &bytes) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE state = nullptr;
  std::array<uint8_t, 32> hash{};
  auto status = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
  if (status >= 0)
    status = BCryptCreateHash(algorithm, &state, nullptr, 0, nullptr, 0, 0);
  if (status >= 0)
    status = BCryptHashData(state, const_cast<PUCHAR>(bytes.data()), ULONG(bytes.size()), 0);
  if (status >= 0)
    status = BCryptFinishHash(state, hash.data(), ULONG(hash.size()), 0);
  if (state)
    BCryptDestroyHash(state);
  if (algorithm)
    BCryptCloseAlgorithmProvider(algorithm, 0);
  check(status >= 0, "cannot compute SHA-256");
  return hex(hash.data(), hash.size());
}
inline void no_reparse(const fs::path &path) {
  fs::path part;
  for (const auto &component : fs::absolute(path)) {
    part /= component;
    DWORD flags = GetFileAttributesW(part.c_str());
    if (flags != INVALID_FILE_ATTRIBUTES)
      check(!(flags & FILE_ATTRIBUTE_REPARSE_POINT), "linked firmware paths are not allowed");
  }
}
inline std::vector<uint8_t> read(const fs::path &path, size_t limit) {
  no_reparse(path);
  Handle file(CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                          FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
  if (!file)
    throw Error("missing_firmware", "", "file unavailable");
  BY_HANDLE_FILE_INFORMATION info{};
  check(GetFileInformationByHandle(file, &info) && !info.nFileSizeHigh &&
            info.nFileSizeLow <= limit &&
            !(info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)),
        "invalid firmware file size/type");
  std::vector<uint8_t> result(info.nFileSizeLow);
  DWORD got = 0;
  check(ReadFile(file, result.data(), DWORD(result.size()), &got, nullptr) && got == result.size(),
        "cannot read firmware file");
  return result;
}
inline void verify_signature(const std::vector<uint8_t> &bytes,
                             const std::vector<uint8_t> &signature,
                             const uint8_t *public_key = catalog_key.data(),
                             size_t key_size = catalog_key.size()) {
  check(signature.size() == 64, "invalid catalog signature size");
  auto hash = unhex(digest(bytes));
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_KEY_HANDLE key = nullptr;
  auto status = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_ECDSA_P256_ALGORITHM, nullptr, 0);
  if (status >= 0)
    status = BCryptImportKeyPair(algorithm, nullptr, BCRYPT_ECCPUBLIC_BLOB, &key,
                                 const_cast<PUCHAR>(public_key), ULONG(key_size), 0);
  if (status >= 0)
    status =
        BCryptVerifySignature(key, nullptr, hash.data(), ULONG(hash.size()),
                              const_cast<PUCHAR>(signature.data()), ULONG(signature.size()), 0);
  if (key)
    BCryptDestroyKey(key);
  if (algorithm)
    BCryptCloseAlgorithmProvider(algorithm, 0);
  if (status < 0)
    throw Error("catalog_untrusted", "", "catalog signature verification failed");
}
inline bool token(const std::string &value, size_t limit = 80) {
  return !value.empty() && value.size() <= limit &&
         std::all_of(value.begin(), value.end(),
                     [](char c) {
                       return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                              (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
                     }) &&
         value != "." && value != "..";
}
inline bool relative(const std::string &value) {
  if (value.empty() || value.size() > 200 || value.front() == '/' || value.back() == '/')
    return false;
  size_t first = 0;
  while (first < value.size()) {
    auto end = value.find('/', first);
    auto part = value.substr(first, end == std::string::npos ? end : end - first);
    if (!token(part))
      return false;
    if (end == std::string::npos)
      break;
    first = end + 1;
  }
  return true;
}
inline bool https_source(const std::string &url) {
  static const char *hosts[] = {"https://kernel.googlesource.com/", "https://gitlab.com/",
                                "https://raw.githubusercontent.com/"};
  return url.size() <= 2048 && url.find_first_of("\r\n\t #@\\") == std::string::npos &&
         std::any_of(std::begin(hosts), std::end(hosts),
                     [&](const char *host) { return url.rfind(host, 0) == 0; });
}
class Catalog {
  json data;

public:
  explicit Catalog(const fs::path &assets) {
    auto raw = read(assets / L"firmware/catalog.json", 1024 * 1024);
    verify_signature(raw, read(assets / L"firmware/catalog.sig", 64));
    std::vector<std::set<std::string>> keys;
    data = json::parse(raw, [&](int, json::parse_event_t event, json &value) {
      if (event == json::parse_event_t::object_start)
        keys.emplace_back();
      if (event == json::parse_event_t::key)
        check(!keys.empty() && keys.back().insert(value.get<std::string>()).second,
              "duplicate catalog field");
      if (event == json::parse_event_t::object_end)
        keys.pop_back();
      return true;
    });
    validate();
  }
  void validate() const {
    check(data.at("schema") == 1 && data.at("generation").is_number_unsigned() &&
              data.at("generation").get<unsigned>() >= minimum_generation,
          "unsupported catalog generation/schema");
    auto &artifacts = data.at("artifacts");
    check(artifacts.is_object() && !artifacts.empty() && artifacts.size() <= 256,
          "invalid artifact count");
    for (auto it = artifacts.begin(); it != artifacts.end(); ++it) {
      auto &a = it.value();
      auto hash = a.at("sha256").get<std::string>();
      check(token(it.key()) && hash.size() == 64 && hash == hex(unhex(hash).data(), 32),
            "invalid artifact identity");
      check(a.at("bytes").is_number_unsigned() && a.at("bytes").get<size_t>() > 0 &&
                a.at("bytes").get<size_t>() <= max_artifact && relative(a.at("path")),
            "invalid artifact size/path");
      if (a.contains("bundled"))
        check(relative(a.at("bundled")), "invalid bundled path");
      check(a.at("sources").is_array() && a.at("sources").size() <= 4 && !a.at("sources").empty(),
            "invalid firmware sources");
      for (auto &source : a.at("sources"))
        check(https_source(source.at("url")) &&
                  (source.at("encoding") == "raw" || source.at("encoding") == "base64"),
              "invalid source URL/encoding");
    }
    std::set<std::string> ids;
    check(data.at("profiles").is_array() && data.at("profiles").size() <= 128,
          "invalid profile count");
    for (auto &p : data.at("profiles")) {
      check(token(p.at("id")) && ids.insert(p.at("id")).second && token(p.at("backend")) &&
                token(p.at("format")),
            "invalid/duplicate profile");
      for (auto key : {"image", "config", "license"})
        check(artifacts.contains(p.at(key).get<std::string>()), "unknown profile artifact");
      check(p.at("match").is_object() && !p.at("match").empty(), "missing chip match constraints");
      for (auto it = p.at("match").begin(); it != p.at("match").end(); ++it) {
        check(it.value().is_array() && !it.value().empty() && it.value().size() <= 64,
              "invalid chip match");
        for (auto &number : it.value()) {
          if (it.key() == "usb")
            check(number.is_array() && number.size() == 2 && number[0].is_number_unsigned() &&
                      number[1].is_number_unsigned() && number[0].get<unsigned>() <= 65535 &&
                      number[1].get<unsigned>() <= 65535,
                  "invalid USB match");
          else
            check(number.is_number_unsigned() && number.get<unsigned>() <= 65535,
                  "invalid chip fact");
        }
      }
    }
  }
  json select(const std::string &backend, const json &facts) const {
    json found;
    size_t specificity = 0;
    bool ambiguous = false;
    for (auto &p : data.at("profiles")) {
      if (p.at("backend") != backend)
        continue;
      bool matches = true;
      for (auto it = p.at("match").begin(); it != p.at("match").end(); ++it)
        if (!facts.contains(it.key()) || std::find(it.value().begin(), it.value().end(),
                                                   facts.at(it.key())) == it.value().end()) {
          matches = false;
          break;
        }
      if (!matches)
        continue;
      size_t score = p.at("match").size();
      if (score > specificity) {
        found = p;
        specificity = score;
        ambiguous = false;
      } else if (score == specificity)
        ambiguous = true;
    }
    if (found.is_null())
      throw Error("unsupported_firmware", "", "no validated firmware profile for these chip facts");
    if (ambiguous)
      throw Error("ambiguous_firmware", "", "more than one firmware profile matches");
    return found;
  }
  const json &profile(const std::string &id) const {
    for (auto &p : data.at("profiles"))
      if (p.at("id") == id)
        return p;
    throw Error("unsupported_firmware", id, "unknown firmware profile");
  }
  const json &artifact(const std::string &name) const { return data.at("artifacts").at(name); }
  const json &snapshot() const { return data; }
};
inline fs::path default_cache() {
  wchar_t buffer[32768];
  DWORD size = GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, 32768);
  check(size && size < 32768, "local application data unavailable");
  return fs::path(buffer) / L"BlueHead/Firmware";
}
class Store {
  const Catalog &catalog;
  fs::path assets, cache;
  bool valid(const std::vector<uint8_t> &bytes, const json &artifact) const {
    return bytes.size() == artifact.at("bytes").get<size_t>() &&
           digest(bytes) == artifact.at("sha256");
  }

public:
  Store(const Catalog &c, fs::path bundled, fs::path directory = {})
      : catalog(c), assets(std::move(bundled)),
        cache(directory.empty() ? default_cache() : std::move(directory)) {}
  fs::path path(const json &a) const { return cache / wide(a.at("sha256").get<std::string>()); }
  std::vector<uint8_t> load(const std::string &name, const std::string &profile_id) const {
    auto &a = catalog.artifact(name);
    std::vector<fs::path> candidates{path(a)};
    if (a.contains("bundled"))
      candidates.push_back(assets / wide(a.at("bundled")));
    bool corrupt = false;
    for (const auto &candidate : candidates) {
      if (!fs::exists(candidate))
        continue;
      try {
        auto bytes = read(candidate, max_artifact);
        if (valid(bytes, a))
          return bytes;
        corrupt = true;
      } catch (const std::exception &) {
        corrupt = true;
      }
    }
    throw Error(corrupt ? "corrupt_firmware" : "missing_firmware", profile_id,
                "verified artifact unavailable: " + name);
  }
  void put(const std::string &name, const std::vector<uint8_t> &bytes) {
    auto &a = catalog.artifact(name);
    check(valid(bytes, a), "import/download digest or length mismatch");
    no_reparse(cache);
    fs::create_directories(cache);
    no_reparse(cache);
    size_t total = 0, count = 0;
    for (const auto &entry : fs::directory_iterator(cache)) {
      no_reparse(entry.path());
      if (entry.is_regular_file())
        total += entry.file_size();
      check(++count <= 512 && total <= 128 * 1024 * 1024, "firmware cache limit reached");
    }
    std::array<uint8_t, 12> nonce{};
    check(BCryptGenRandom(nullptr, nonce.data(), nonce.size(), BCRYPT_USE_SYSTEM_PREFERRED_RNG) >=
              0,
          "cannot create cache transaction");
    auto temporary = cache / wide("part-" + hex(nonce.data(), nonce.size()));
    Handle file(CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                            FILE_ATTRIBUTE_NORMAL, nullptr));
    check(bool(file), "cannot create cache file");
    try {
      DWORD written = 0;
      check(WriteFile(file, bytes.data(), DWORD(bytes.size()), &written, nullptr) &&
                written == bytes.size() && FlushFileBuffers(file),
            "cache write failed");
      file.reset();
      no_reparse(path(a));
      check(MoveFileExW(temporary.c_str(), path(a).c_str(),
                        MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH),
            "cannot commit cache file");
    } catch (...) {
      file.reset();
      DeleteFileW(temporary.c_str());
      throw;
    }
  }
};
} // namespace bh::firmware
