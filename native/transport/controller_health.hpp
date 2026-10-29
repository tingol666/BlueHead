/* Runtime controller failure signals, separate from descriptor capabilities.
 * SPDX-License-Identifier: Apache-2.0. No USB ownership, I/O or audio work. */
#pragma once
#include <array>
#include <atomic>
#include <cstdint>
#include <cstring>
namespace bh {
enum class ControllerSignal { Normal, HardwareError, Restarted };
inline ControllerSignal controller_signal(const uint8_t *p, uint32_t n,
                                           bool operational, bool intel_boot_events) {
  if (!operational || !p || n < 2 || n != 2u + p[1]) return ControllerSignal::Normal;
  if (p[0] == 0x10 && n == 3) return ControllerSignal::HardwareError;
  // This is an established vendor boot-completion format, not a generic test
  // of 0xFF. Other vendors' unrelated event 0x02 must never reset a session.
  if (intel_boot_events && p[0] == 0xff && n == 9 && p[2] == 2)
    return ControllerSignal::Restarted;
  return ControllerSignal::Normal;
}

// The terminal event cannot be forwarded after the HCI owner has retired. Keep
// its exact, bounded evidence instead of losing it at the classifier's return.
// Sole USB thread publishes once; any diagnostic reader may copy after acquire.
// No allocation, lock, USB I/O or audio processing occurs on this path.
class ControllerEvidence {
  std::array<uint8_t,9> bytes{};
  std::atomic<uint32_t> published{0};
public:
  bool record(ControllerSignal signal, const uint8_t *p, uint32_t n) noexcept {
    if (signal==ControllerSignal::Normal || !p || n<2 || n>bytes.size() ||
        n!=2u+p[1] || published.load(std::memory_order_relaxed)) return false;
    std::memcpy(bytes.data(),p,n);
    published.store(n,std::memory_order_release);
    return true;
  }
  uint32_t copy(uint8_t *out, uint32_t capacity) const noexcept {
    auto n=published.load(std::memory_order_acquire);
    if (!out || !n || capacity<n) return 0;
    std::memcpy(out,bytes.data(),n);
    return n;
  }
};
} // namespace bh
