/* Control-side native resource ownership. Library references must outlive this owner. */
#pragma once
namespace bh {
class NativeResource {
  void *value = nullptr;
  void (*release)(void *) = nullptr;

public:
  NativeResource() = default;
  NativeResource(const NativeResource &) = delete;
  NativeResource &operator=(const NativeResource &) = delete;
  ~NativeResource() { reset(); }
  void reset(void *next = nullptr, void (*destroy)(void *) = nullptr) noexcept {
    if (value && release)
      release(value);
    value = next;
    release = destroy;
  }
  void *get() const noexcept { return value; }
};
} // namespace bh
