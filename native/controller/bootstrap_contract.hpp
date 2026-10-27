/* Vendor implementations borrow the single physical transport; they never open USB. */
#pragma once
#include "runtime_common.hpp"
#include <functional>
namespace bh {
struct BootstrapIO {
  std::function<void(uint16_t, const std::vector<uint8_t> &, bool)> write;
  std::function<void()> pump;
  std::function<void(const char *, unsigned)> progress;
};
class ControllerBootstrap {
public:
  bool bulk_events;
  explicit ControllerBootstrap(bool events_in_bulk = false) : bulk_events(events_in_bulk) {}
  virtual ~ControllerBootstrap() = default;
  virtual const char *name() const = 0;
  virtual void event(const uint8_t *, uint32_t) = 0;
  virtual void run(const fs::path &) = 0;
};
} // namespace bh
