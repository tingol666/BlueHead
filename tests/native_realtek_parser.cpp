#include "realtek_bootstrap.hpp"
#include <iostream>
int wmain(int count, wchar_t **values) {
  if (count != 3 && count != 5)
    return 2;
  auto raw = bh::firmware::read(values[1], bh::firmware::max_artifact);
  auto config = bh::firmware::read(values[2], 65536);
  bh::json checks = bh::json::object();
  unsigned rom = count == 5 ? std::stoul(values[3]) : 0;
  unsigned project = count == 5 ? std::stoul(values[4]) : 14;
  auto image = bh::RealtekImage::parse(raw, config, rom, project);
  checks["real_patch_and_board_config"] =
      image.payload.size() > config.size() &&
      std::equal(config.begin(), config.end(), image.payload.end() - config.size());
  auto reject = [&](const char *name, std::vector<uint8_t> bytes, std::vector<uint8_t> settings,
                    unsigned selected_rom = 0, unsigned selected_project = 0) {
    try {
      bh::RealtekImage::parse(bytes, settings, selected_rom,
                              selected_project ? selected_project : project);
      checks[name] = false;
    } catch (const std::exception &) {
      checks[name] = true;
    }
  };
  auto changed = raw;
  changed[0] ^= 1;
  reject("wrong_format", changed, config);
  changed = raw;
  changed.back() ^= 1;
  reject("missing_extension", changed, config);
  changed = raw;
  changed[12] = changed[13] = 255;
  reject("oversized_table", changed, config);
  changed = raw;
  changed[16] = changed[14];
  changed[17] = changed[15];
  reject("duplicate_chip", changed, config);
  changed = raw;
  changed[22] = changed[23] = changed[24] = changed[25] = 255;
  reject("offset_overflow", changed, config);
  auto bad_config = config;
  bad_config[0] ^= 1;
  reject("wrong_config_magic", raw, bad_config);
  bad_config = config;
  bad_config.push_back(1);
  reject("config_size_mismatch", raw, bad_config);
  reject("wrong_rom", raw, config, 20);
  reject("wrong_project", raw, config, rom, project + 1);
  for (size_t size = 0; size < 22; ++size)
    reject(("truncated_" + std::to_string(size)).c_str(),
           std::vector<uint8_t>(raw.begin(), raw.begin() + size), config);
  checks["fragment_wrap_preserves_protocol_sequence"] =
      bh::RealtekImage::fragment_index(0, false) == 0 &&
      bh::RealtekImage::fragment_index(127, false) == 127 &&
      bh::RealtekImage::fragment_index(128, false) == 1 &&
      bh::RealtekImage::fragment_index(254, false) == 127 &&
      bh::RealtekImage::fragment_index(255, true) == 0x81;
  bool passed =
      std::all_of(checks.begin(), checks.end(), [](const bh::json &v) { return v == true; });
  std::cout << bh::json({{"passed", passed},
                         {"checks", checks},
                         {"payload_sha256", bh::firmware::digest(image.payload)}})
                   .dump()
            << '\n';
  return passed ? 0 : 1;
}
