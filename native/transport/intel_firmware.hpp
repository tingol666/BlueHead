/* Intel USB RAM firmware bootstrap, before the host receives HCI ownership.
 * Apache-2.0. Protocol reference: Google Bumble's Apache-2.0 Intel driver;
 * see docs/INTEL_FIRMWARE.md. No firmware is modified or written to flash. */
#pragma once
#include "runtime_common.hpp"
#include "bootstrap_contract.hpp"
#include "catalog.hpp"
#include <bcrypt.h>
#include <functional>

namespace bh {
struct IntelVersion {
  uint32_t cnvi = 0, cnvr = 0;
  uint8_t platform = 0, variant = 0, mode = 0, engine = 0xff;
  static uint32_t u32(const uint8_t *p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
  }
  static uint16_t identity(uint32_t v) {
    return uint16_t(((v & 0xf) << 12) | ((v >> 4) & 0xf) | ((v >> 4) & 0xf0) | ((v >> 16) & 0xf00));
  }
  static IntelVersion parse(const std::vector<uint8_t> &reply) {
    require(!reply.empty() && reply[0] == 0, "Intel firmware: version query rejected");
    IntelVersion v;
    std::array<bool, 256> seen{};
    for (size_t i = 1; i < reply.size();) {
      require(reply.size() - i >= 2, "Intel firmware: truncated version TLV");
      auto type = reply[i++], size = reply[i++];
      require(size <= reply.size() - i, "Intel firmware: truncated version value");
      if (!type) {
        require(!size && i == reply.size(), "Intel firmware: invalid version terminator");
        break;
      }
      require(!seen[type], "Intel firmware: duplicate version field");
      seen[type] = true;
      const auto *p = reply.data() + i;
      if (type == 0x10 || type == 0x11 || type == 0x12) {
        require(size == 4, "Intel firmware: invalid hardware field");
        uint32_t raw = u32(p);
        if (type == 0x10)
          v.cnvi = raw;
        if (type == 0x11)
          v.cnvr = raw;
        if (type == 0x12) {
          v.platform = uint8_t(raw >> 8);
          v.variant = (raw >> 16) & 0x3f;
        }
      } else if (type == 0x1c || type == 0x2f) {
        require(size == 1, "Intel firmware: invalid boot state field");
        if (type == 0x1c)
          v.mode = *p;
        else
          v.engine = *p;
      }
      i += size;
    }
    require(seen[0x1c] && (v.mode == 1 || v.mode == 3),
            "Intel firmware: unsupported controller boot state");
    if (v.mode == 1)
      require(seen[0x10] && seen[0x11] && seen[0x12] && seen[0x2f] && v.platform == 0x37 &&
                  (v.variant == 0x17 || v.variant == 0x18 || v.variant == 0x19) && v.engine <= 1,
              "Intel firmware: unsupported secure boot hardware");
    return v;
  }
  std::string name() const {
    char text[32];
    std::snprintf(text, sizeof(text), "ibt-%04x-%04x", identity(cnvi), identity(cnvr));
    return text;
  }
};

struct IntelImage {
  struct Fragment {
    uint8_t type;
    size_t offset, size;
  };
  std::vector<uint8_t> sfi, ddc;
  std::vector<Fragment> fragments;
  std::vector<std::pair<size_t, size_t>> configs;
  uint32_t boot_address = 0;

  // Validate the ENTIRE image and configuration before sending any fragment.
  void validate(uint8_t engine) {
    require(engine <= 1 && sfi.size() > 964 && sfi.size() <= 4 * 1024 * 1024,
            "Intel firmware: invalid image size or secure boot engine");
    require(IntelVersion::u32(sfi.data() + 8) == 0x10000 && sfi[644] == 6 &&
                IntelVersion::u32(sfi.data() + 652) == 0x20000,
            "Intel firmware: unsupported signed image header");
    fragments.clear();
    configs.clear();
    auto add = [this](uint8_t type, size_t offset, size_t size) {
      while (size) {
        size_t n = std::min(size, size_t(252));
        fragments.push_back({type, offset, n});
        offset += n;
        size -= n;
      }
    };
    add(0, engine ? 644 : 0, 128);
    add(3, engine ? 772 : 128, engine ? 96 : 256);
    add(2, engine ? 868 : 388, engine ? 96 : 256);
    bool boot = false;
    size_t group = 964;
    for (size_t i = group; i < sfi.size();) {
      require(sfi.size() - i >= 3, "Intel firmware: truncated image command");
      size_t size = 3 + size_t(sfi[i + 2]);
      require(size <= sfi.size() - i, "Intel firmware: truncated command parameters");
      if (le16(sfi.data() + i) == 0xfc0e) {
        require(!boot && size == 10, "Intel firmware: invalid boot parameters");
        boot_address = IntelVersion::u32(sfi.data() + i + 3);
        boot = true;
      }
      i += size;
      require(i - group <= 4096, "Intel firmware: unaligned command group");
      if ((i - group) % 4 == 0) {
        add(1, group, i - group);
        group = i;
      }
    }
    require(boot && group == sfi.size(), "Intel firmware: missing boot parameters or image tail");
    require(!ddc.empty() && ddc.size() <= 65536, "Intel firmware: missing device configuration");
    for (size_t i = 0; i < ddc.size();) {
      size_t size = 1 + size_t(ddc[i]);
      require(size >= 3 && size <= 255 && size <= ddc.size() - i,
              "Intel firmware: malformed device configuration");
      configs.emplace_back(i, size);
      i += size;
    }
  }

  static IntelImage load(const fs::path &assets, const IntelVersion &v) {
    firmware::Catalog catalog(assets);
    auto profile = catalog.select("intel-tlv", {{"variant", v.variant},
                                                {"cnvi", IntelVersion::identity(v.cnvi)},
                                                {"cnvr", IntelVersion::identity(v.cnvr)}});
    std::string id = profile.at("id");
    firmware::Store store(catalog, assets);
    store.load(profile.at("license"), id);
    IntelImage image;
    image.sfi = store.load(profile.at("image"), id);
    image.ddc = store.load(profile.at("config"), id);
    image.validate(v.engine);
    return image;
  }
};

// All methods run on the existing USB event owner. Commands/credits here are
// private to bootstrap; they are never mixed with Bumble's operational ledger.
class IntelFirmware final : public ControllerBootstrap {
  std::function<void(uint16_t, const std::vector<uint8_t> &, bool)> write;
  std::function<void()> pump;
  std::function<void(const char *, unsigned)> progress;
  uint16_t expected = 0;
  bool replied = false, download_done = false, boot_done = false;
  std::vector<uint8_t> reply;
  std::string event_error;
  double deadline = 0;
  template <class Predicate> void wait(Predicate predicate, double duration, const char *stage) {
    auto until = std::min(deadline, seconds() + duration);
    while (!predicate()) {
      if (!event_error.empty())
        throw std::runtime_error(event_error);
      if (seconds() >= until)
        throw std::runtime_error(std::string("Intel firmware: timeout at ") + stage);
      pump();
    }
    if (!event_error.empty())
      throw std::runtime_error(event_error);
  }
  std::vector<uint8_t> command(uint16_t op, const std::vector<uint8_t> &data = {},
                               bool bulk = false) {
    expected = op;
    replied = false;
    reply.clear();
    write(op, data, bulk);
    wait([this] { return replied; }, 2, "command acknowledgement");
    expected = 0;
    return reply;
  }
  void success(const std::vector<uint8_t> &value) {
    require(!value.empty() && value[0] == 0, "Intel firmware: controller rejected a command");
  }

public:
  static bool supported(uint16_t vid, uint16_t pid) {
    return vid == 0x8087 && (pid == 0x0032 || pid == 0x0033 || pid == 0x0036);
  }
  IntelFirmware(decltype(write) w, decltype(pump) p, decltype(progress) report)
      : ControllerBootstrap(true), write(std::move(w)), pump(std::move(p)),
        progress(std::move(report)) {}
  const char *name() const override { return "intel"; }
  void event(const uint8_t *p, uint32_t n) override {
    if (n < 2 || p[1] != n - 2) {
      event_error = "Intel firmware: malformed HCI event";
      return;
    }
    if (p[0] == 0x0e && n >= 6 && le16(p + 3) == expected && expected) {
      if (replied || !p[2]) {
        event_error = "Intel firmware: invalid command acknowledgement";
        return;
      }
      reply.assign(p + 5, p + n);
      replied = true;
    } else if (p[0] == 0x0f && n == 6 && le16(p + 4) == expected && p[2]) {
      event_error = "Intel firmware: command status rejected";
    } else if (p[0] == 0xff && n >= 3) {
      if (p[2] == 6) {
        // Some AX211 bootloaders report opcode=0 on successful verification.
        // The result/status decide success; opcode only identifies a failure.
        if (n != 7 || p[3] || p[6])
          event_error = "Intel firmware: signed image validation response " + hex(p, n);
        else
          download_done = true;
      } else if (p[2] == 2) {
        // Source/reset reason/DDC status describe this boot; they are not a
        // command status code. Verify OPERATIONAL via Read Version below.
        if (n != 9)
          event_error = "Intel firmware: invalid boot completion";
        else
          boot_done = true;
      }
    } else if (p[0] == 0x10)
      event_error = "Intel firmware: controller hardware error";
  }
  void run(const fs::path &assets) override {
    deadline = seconds() + 60;
    progress("probing", 0);
    auto reset = command(0x0c03);
    require(!reset.empty() && (reset[0] == 0 || reset[0] == 1),
            "Intel firmware: unexpected controller reset result");
    auto v = IntelVersion::parse(command(0xfc05, {0xff}));
    if (v.mode == 3) {
      success(reset);
      bulk_events = false;
      progress("already_loaded", 100);
      return;
    }
    progress("validating", 0);
    auto image = IntelImage::load(assets, v);
    progress("downloading", 0);
    for (size_t i = 0; i < image.fragments.size(); i++) {
      const auto &f = image.fragments[i];
      std::vector<uint8_t> data{f.type};
      data.insert(data.end(), image.sfi.begin() + f.offset, image.sfi.begin() + f.offset + f.size);
      success(command(0xfc09, data, true));
      if (i % 32 == 0)
        progress("downloading", unsigned(100 * (i + 1) / image.fragments.size()));
    }
    wait([this] { return download_done; }, 5, "signed image validation");
    progress("booting", 100);
    std::vector<uint8_t> boot{0, 1, 0, 1, 0, 0, 0, 0};
    for (unsigned i = 0; i < 4; i++)
      boot[4 + i] = uint8_t(image.boot_address >> (i * 8));
    // Intel's soft boot completes with a vendor event, not Command Complete.
    expected = 0;
    boot_done = false;
    write(0xfc01, boot, false);
    wait([this] { return boot_done; }, 5, "operational firmware boot");
    bulk_events = false;
    auto operational = IntelVersion::parse(command(0xfc05, {0xff}));
    require(operational.mode == 3, "Intel firmware: controller did not enter operational mode");
    progress("configuring", 100);
    for (const auto &c : image.configs)
      success(command(0xfc8b, std::vector<uint8_t>(image.ddc.begin() + c.first,
                                                   image.ddc.begin() + c.first + c.second)));
    success(command(0x0c03));
    progress("loaded", 100);
  }
};
} // namespace bh
