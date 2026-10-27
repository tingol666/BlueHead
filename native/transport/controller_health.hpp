/* Runtime controller failure signals, separate from descriptor capabilities.
 * SPDX-License-Identifier: Apache-2.0. No USB ownership, I/O or audio work. */
#pragma once
#include <cstdint>
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
} // namespace bh
