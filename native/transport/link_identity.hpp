/* Handle identity owned by the one HCI event producer. Apache-2.0.
 * A reused numeric handle is a different connection. Readers never lock the
 * USB owner; generations are copied with queued signalling packets. */
#pragma once
#include <array>
#include <atomic>
#include <cstdint>
namespace bh {
class LinkIdentity {
  std::array<std::atomic<uint64_t>,4096> identities{};
  std::atomic<uint64_t> sequence{0};
public:
  uint64_t open(uint16_t handle) noexcept {
    if(handle>0xeff) return 0;
    auto value=sequence.fetch_add(1,std::memory_order_relaxed)+1;
    if(!value) value=sequence.fetch_add(1,std::memory_order_relaxed)+1;
    identities[handle].store(value,std::memory_order_release);
    return value;
  }
  void close(uint16_t handle) noexcept {
    if(handle<=0xeff) identities[handle].store(0,std::memory_order_release);
  }
  uint64_t get(uint16_t handle) const noexcept {
    return handle<=0xeff?identities[handle].load(std::memory_order_acquire):0;
  }
  uint64_t last() const noexcept { return sequence.load(std::memory_order_acquire); }
  void clear() noexcept { for(auto &entry:identities) entry.store(0,std::memory_order_release); }
};
} // namespace bh
