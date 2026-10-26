/* HCI completion filtering for the sole native USB owner. Apache-2.0. */
#pragma once
#include "runtime_common.hpp"
namespace bh {
/* Bumble retains the one command/ACL credit ledger and response matching for
 * signalling. Native SCO uses USB pacing, not Bumble's ACL packet queue.
 * Consume SCO-only completion reports here so a paused control process cannot
 * accumulate one signalling notification per real-time voice packet. */
template <class IsSco>
inline bool filter_completions(const uint8_t *input, uint32_t size,
                               std::array<uint8_t, 257> &output, uint32_t &length, IsSco is_sco,
                               uint64_t &completed) {
  length = size;
  if (!size || input[0] != 0x13)
    return false;
  if (size < 3 || input[1] != size - 2 || size != 3u + input[2] * 4u) {
    length = 0;
    return true;
  }
  output[0] = 0x13;
  output[2] = 0;
  uint32_t at = 3;
  for (uint32_t i = 0; i < input[2]; i++) {
    const uint8_t *p = input + 3 + i * 4;
    uint16_t handle = le16(p) & 0xfff;
    if (is_sco(handle))
      completed += le16(p + 2);
    else {
      std::memcpy(output.data() + at, p, 4);
      at += 4;
      output[2]++;
    }
  }
  length = output[2] ? at : 0;
  output[1] = static_cast<uint8_t>(at - 2);
  return true;
}
} // namespace bh
