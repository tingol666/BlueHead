/* Acquisition runs in a separate, non-realtime process, without HCI ownership. */
#pragma once
#include "catalog.hpp"
#include <winhttp.h>
#include <wincrypt.h>
namespace bh::firmware {
struct Internet {
  HINTERNET value = nullptr;
  explicit Internet(HINTERNET p) : value(p) { check(value != nullptr, "HTTPS request failed"); }
  ~Internet() { WinHttpCloseHandle(value); }
  operator HINTERNET() const { return value; }
  Internet(const Internet &) = delete;
};
inline std::vector<uint8_t> download(std::string url, size_t maximum) {
  Internet session(WinHttpOpen(L"BlueHead-Firmware/1", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, nullptr,
                               nullptr, 0));
  check(WinHttpSetTimeouts(session, 10000, 10000, 15000, 15000), "cannot set HTTPS time limits");
  double deadline = seconds() + 60;
  for (unsigned redirects = 0; redirects <= 3; ++redirects) {
    check(https_source(url), "untrusted redirect host or URL");
    std::wstring address = wide(url);
    URL_COMPONENTS parts{};
    parts.dwStructSize = sizeof(parts);
    parts.dwHostNameLength = parts.dwUrlPathLength = parts.dwExtraInfoLength = DWORD(-1);
    check(WinHttpCrackUrl(address.c_str(), DWORD(address.size()), 0, &parts) &&
              parts.nScheme == INTERNET_SCHEME_HTTPS && parts.nPort == INTERNET_DEFAULT_HTTPS_PORT,
          "invalid HTTPS source");
    std::wstring host(parts.lpszHostName, parts.dwHostNameLength);
    std::wstring route(parts.lpszUrlPath, parts.dwUrlPathLength);
    if (parts.dwExtraInfoLength)
      route.append(parts.lpszExtraInfo, parts.dwExtraInfoLength);
    Internet connection(WinHttpConnect(session, host.c_str(), INTERNET_DEFAULT_HTTPS_PORT, 0));
    Internet request(WinHttpOpenRequest(connection, L"GET", route.c_str(), nullptr,
                                        WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                        WINHTTP_FLAG_SECURE));
    DWORD redirects_off = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
    check(WinHttpSetOption(request, WINHTTP_OPTION_REDIRECT_POLICY, &redirects_off,
                           sizeof(redirects_off)),
          "cannot restrict HTTPS redirects");
    check(WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, nullptr, 0, 0, 0) &&
              WinHttpReceiveResponse(request, nullptr),
          "HTTPS source unavailable");
    DWORD status = 0, size = sizeof(status);
    check(WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                              WINHTTP_HEADER_NAME_BY_INDEX, &status, &size,
                              WINHTTP_NO_HEADER_INDEX),
          "missing HTTP status");
    if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
      std::array<wchar_t, 2049> location{};
      size = DWORD(location.size() * sizeof(wchar_t));
      check(redirects < 3 &&
                WinHttpQueryHeaders(request, WINHTTP_QUERY_LOCATION, WINHTTP_HEADER_NAME_BY_INDEX,
                                    location.data(), &size, WINHTTP_NO_HEADER_INDEX),
            "invalid HTTPS redirect");
      url = utf8(location.data());
      continue;
    }
    check(status == 200, "firmware source returned an error");
    std::vector<uint8_t> bytes;
    std::array<uint8_t, 16384> chunk{};
    for (;;) {
      DWORD got = 0;
      check(seconds() < deadline &&
                WinHttpReadData(request, chunk.data(), DWORD(chunk.size()), &got),
            "firmware download timeout/read error");
      if (!got)
        return bytes;
      check(got <= maximum - bytes.size(), "firmware response exceeded its bound");
      bytes.insert(bytes.end(), chunk.begin(), chunk.begin() + got);
    }
  }
  throw Error("source_unavailable", "", "too many HTTPS redirects");
}
inline std::vector<uint8_t> decode(const std::vector<uint8_t> &bytes, const json &artifact,
                                   const json &source) {
  if (source.at("encoding") == "raw")
    return bytes;
  DWORD size = 0;
  check(CryptStringToBinaryA(reinterpret_cast<const char *>(bytes.data()), DWORD(bytes.size()),
                             CRYPT_STRING_BASE64 | CRYPT_STRING_STRICT, nullptr, &size, nullptr,
                             nullptr) &&
            size == artifact.at("bytes").get<size_t>(),
        "invalid base64 firmware length");
  std::vector<uint8_t> result(size);
  check(CryptStringToBinaryA(reinterpret_cast<const char *>(bytes.data()), DWORD(bytes.size()),
                             CRYPT_STRING_BASE64 | CRYPT_STRING_STRICT, result.data(), &size,
                             nullptr, nullptr),
        "invalid base64 firmware");
  return result;
}
// An offline upgrade/import operation. No directory recursion, USB ownership,
// controller selection or downloads; only catalogued data can enter the cache.
inline json migrate(const fs::path &assets, const fs::path &cache, const fs::path &imported) {
  check(!imported.empty(), "migration requires an explicit source directory");
  no_reparse(imported);
  Catalog catalog(assets);
  Store store(catalog, assets, cache);
  json report = {{"artifacts", json::array()}, {"ready_profiles", json::array()}};
  std::set<std::string> available;
  for (auto it = catalog.snapshot().at("artifacts").begin();
       it != catalog.snapshot().at("artifacts").end(); ++it) {
    const auto &a = it.value();
    std::string source = "verified_local";
    bool ready = false;
    try {
      store.load(it.key(), "");
      ready = true;
    } catch (const Error &) {
    }
    if (!ready) {
      std::vector<fs::path> candidates{imported / wide(a.at("path")),
                                       imported / fs::path(wide(a.at("path"))).filename()};
      if (a.contains("bundled"))
        candidates.push_back(imported / wide(a.at("bundled")));
      for (const auto &candidate : candidates) {
        if (!fs::exists(candidate))
          continue;
        store.put(it.key(), read(candidate, max_artifact));
        store.load(it.key(), "");
        ready = true;
        source = "verified_import";
        break;
      }
    }
    if (ready)
      available.insert(it.key());
    report["artifacts"].push_back(
        {{"id", it.key()}, {"ready", ready}, {"source", ready ? source : "unavailable"}});
  }
  for (const auto &profile : catalog.snapshot().at("profiles"))
    if (available.count(profile.at("image")) && available.count(profile.at("config")) &&
        available.count(profile.at("license")))
      report["ready_profiles"].push_back(profile.at("id"));
  return report;
}
inline json ensure(const fs::path &assets, const fs::path &cache, const std::string &id,
                   bool network, const fs::path &imported = {}) {
  Catalog catalog(assets);
  Store store(catalog, assets, cache);
  const auto &profile = catalog.profile(id);
  json report = {{"profile", id}, {"ready", false}, {"artifacts", json::array()}};
  for (auto role : {"license", "image", "config"}) {
    auto name = profile.at(role).get<std::string>();
    const auto &artifact = catalog.artifact(name);
    std::string source = "verified_local";
    try {
      store.load(name, id);
    } catch (const Error &) {
      bool done = false;
      if (!imported.empty()) {
        auto path = imported / fs::path(wide(artifact.at("path"))).filename();
        if (fs::exists(path)) {
          store.put(name, read(path, max_artifact));
          source = "verified_import";
          done = true;
        }
      }
      if (!done && network) {
        for (auto &remote : artifact.at("sources")) {
          try {
            size_t maximum = artifact.at("bytes").get<size_t>();
            if (remote.at("encoding") == "base64")
              maximum = ((maximum + 2) / 3) * 4 + 1024;
            store.put(name, decode(download(remote.at("url"), maximum), artifact, remote));
            source = remote.at("url");
            done = true;
            break;
          } catch (const std::exception &) { /* Try only the other signed source. */
          }
        }
      }
      if (!done)
        throw Error(
            "source_unavailable", id,
            "cannot acquire verified " + name +
                "; import an original driver package or use original-driver initialization");
    }
    store.load(name, id); // Recheck committed bytes; completion never trusts a download flag.
    report["artifacts"].push_back(
        {{"role", role}, {"sha256", artifact.at("sha256")}, {"source", source}});
  }
  report["ready"] = true;
  return report;
}
} // namespace bh::firmware
