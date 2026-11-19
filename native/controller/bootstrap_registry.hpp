#pragma once
#include "bootstrap_contract.hpp"
#include "controller_initializer.hpp"
namespace bh {
inline std::unique_ptr<ControllerBootstrap> controller_bootstrap(uint16_t vid, uint16_t pid,
                                                                 BootstrapIO io,
                                                                 ControllerAddressProfile profile = {},
                                                                 bool restore_address = false) {
  return std::make_unique<ControllerInitializer>(vid, pid, std::move(io), std::move(profile), restore_address);
}
} // namespace bh
