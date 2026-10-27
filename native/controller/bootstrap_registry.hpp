#pragma once
#include "bootstrap_contract.hpp"
#include "intel_firmware.hpp"
namespace bh {
inline std::unique_ptr<ControllerBootstrap> controller_bootstrap(uint16_t vid, uint16_t pid,
                                                                 BootstrapIO io) {
  if (IntelFirmware::supported(vid, pid))
    return std::make_unique<IntelFirmware>(std::move(io.write), std::move(io.pump),
                                           std::move(io.progress));
  return nullptr;
}
} // namespace bh
