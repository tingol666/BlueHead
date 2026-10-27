/* Apache-2.0. Protocol/model reference: Google Bumble drivers/rtk.py.
 * v1 epatch only; other formats use original-driver initialization.
 * Fragment sequence is 0,1..127,1..127; a final empty fragment is required
 * for an exact multiple of 252. See docs/COMPATIBILITY.md. */
#pragma once
#include "hci_probe.hpp"
#include <set>
namespace bh {
struct RealtekImage {
  std::vector<uint8_t> payload;
  uint32_t version = 0;
  static uint32_t u32(const uint8_t *p) { return uint32_t(le16(p)) | uint32_t(le16(p + 2)) << 16; }
  static uint8_t fragment_index(size_t i, bool last) {
    return uint8_t((i ? 1 + (i - 1) % 127 : 0) | (last ? 0x80 : 0));
  }
  static RealtekImage parse(const std::vector<uint8_t> &image, const std::vector<uint8_t> &config,
                            unsigned rom_version, unsigned project) {
    firmware::check(image.size() >= 22 && image.size() <= firmware::max_artifact &&
                        std::memcmp(image.data(), "Realtech", 8) == 0 &&
                        u32(image.data() + image.size() - 4) == 0x77fd0451,
                    "unsupported Realtek firmware header/extension");
    size_t count = le16(image.data() + 12), table_end = 14 + count * 8;
    firmware::check(count > 0 && count <= 256 && table_end <= image.size() - 4,
                    "invalid Realtek patch table");
    size_t tail = image.size() - 4;
    bool identified = false;
    for (unsigned instruction = 0; tail >= table_end + 2 && instruction < 256; ++instruction) {
      unsigned size = image[tail - 2], opcode = image[tail - 1];
      tail -= 2;
      if (opcode == 0xff)
        break;
      firmware::check(size && size <= tail - table_end, "invalid Realtek extension instruction");
      tail -= size;
      if (opcode == 0) {
        firmware::check(size == 1 && image[tail] == project, "Realtek project does not match chip");
        identified = true;
        break;
      }
    }
    firmware::check(identified, "missing Realtek project identity");
    RealtekImage result;
    result.version = u32(image.data() + 8);
    std::set<unsigned> chips;
    std::vector<std::pair<size_t, size_t>> ranges;
    for (size_t n = 0; n < count; ++n) {
      unsigned chip = le16(image.data() + 14 + n * 2);
      size_t size = le16(image.data() + 14 + count * 2 + n * 2);
      size_t offset = u32(image.data() + 14 + count * 4 + n * 4);
      firmware::check(chips.insert(chip).second && size >= 8 && offset >= table_end &&
                          offset <= tail && size <= tail - offset,
                      "invalid Realtek patch range/identity");
      for (auto range : ranges)
        firmware::check(offset >= range.second || offset + size <= range.first,
                        "overlapping Realtek patches");
      ranges.emplace_back(offset, offset + size);
      if (chip == rom_version + 1) {
        result.payload.assign(image.begin() + offset, image.begin() + offset + size);
        std::copy(image.begin() + 8, image.begin() + 12, result.payload.end() - 4);
      }
    }
    firmware::check(!result.payload.empty(), "no Realtek patch for this ROM revision");
    firmware::check(config.size() >= 6 && config.size() <= 65536 &&
                        u32(config.data()) == 0x8723ab55 &&
                        le16(config.data() + 4) == config.size() - 6,
                    "invalid Realtek board configuration");
    std::set<unsigned> addresses;
    for (size_t at = 6; at < config.size();) {
      firmware::check(config.size() - at >= 3, "truncated Realtek configuration entry");
      size_t size = config[at + 2];
      firmware::check(size && size <= config.size() - at - 3 &&
                          addresses.insert(le16(config.data() + at)).second,
                      "invalid Realtek configuration entry");
      at += size + 3;
    }
    result.payload.insert(result.payload.end(), config.begin(), config.end());
    return result;
  }
};
class RealtekBootstrap final : public BootstrapCommands {
  ControllerVersion initial;
  uint16_t vid, pid;

public:
  RealtekBootstrap(BootstrapIO io, ControllerVersion v, uint16_t vendor, uint16_t product)
      : BootstrapCommands(std::move(io)), initial(v), vid(vendor), pid(product) {}
  const char *name() const override { return "realtek"; }
  void run(const fs::path &assets) override {
    firmware::Catalog catalog(assets);
    auto profile = catalog.select("realtek", initial.facts(vid, pid));
    std::string id = profile.at("id");
    if (profile.at("format") != "realtek-epatch-v1")
      throw firmware::Error("unsupported_firmware", id,
                            "Realtek image format requires original-driver initialization");
    if (profile.at("format") != "realtek-epatch-v1")
      throw firmware::Error("unsupported_firmware", id,
                            "Realtek image format requires original-driver initialization");
    firmware::Store store(catalog, assets);
    io.progress("validating", 0);
    store.load(profile.at("license"), id);
    auto bytes = store.load(profile.at("image"), id), config = store.load(profile.at("config"), id);
    auto rom = command(0xfc6d);
    if (rom.size() != 2)
      throw firmware::Error("hci_protocol", id, "invalid Realtek ROM revision response");
    auto image = RealtekImage::parse(bytes, config, rom[1], profile.at("project"));
    size_t fragments = image.payload.size() / 252 + 1;
    for (size_t i = 0; i < fragments; ++i) {
      size_t first = i * 252, size = std::min<size_t>(252, image.payload.size() - first);
      uint8_t index = RealtekImage::fragment_index(i, i + 1 == fragments);
      std::vector<uint8_t> fragment{index};
      fragment.insert(fragment.end(), image.payload.begin() + first,
                      image.payload.begin() + first + size);
      auto reply = command(0xfc20, fragment);
      if (reply.size() != 2 || (reply[1] & 0x7f) != (index & 0x7f))
        throw firmware::Error("hci_protocol", id, "invalid Realtek download acknowledgement");
      io.progress("downloading", unsigned((i + 1) * 100 / fragments));
    }
    command(0x0c03);
    auto loaded = ControllerVersion::parse(command(0x1001));
    if (loaded.manufacturer != initial.manufacturer ||
        ((loaded.revision << 16) | loaded.rom) != image.version)
      throw firmware::Error("hci_not_initialized", id,
                            "Realtek firmware did not enter the expected operational version");
    io.progress("loaded", 100);
  }
};
} // namespace bh
