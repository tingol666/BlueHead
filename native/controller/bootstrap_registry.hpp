#pragma once
#include "bootstrap_contract.hpp"
#include "controller_initializer.hpp"
namespace bh {
inline std::unique_ptr<ControllerBootstrap> controller_bootstrap(uint16_t vid, uint16_t pid,
                                                                 BootstrapIO io) {
  return std::make_unique<ControllerInitializer>(vid, pid, std::move(io));
}
} // namespace bh
