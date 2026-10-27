/* Single native libusb/HCI owner. Apache-2.0. No Python callbacks or PCM IPC.
 */
#pragma once
#include "runtime_common.hpp"
#include "runtime_hci_events.hpp"
#include "hci_dispatch.hpp"
#include "bootstrap_registry.hpp"
#include "controller_identity.hpp"
#include <bcrypt.h>
#include <functional>
#include <libusb.h>
#include <map>
namespace bh {
// clang-format off
#define USB_APIS(X) \
  X(libusb_init) \
  X(libusb_exit) \
  X(libusb_get_device_list) \
  X(libusb_free_device_list) \
  X(libusb_get_device_descriptor) \
  X(libusb_get_session_data) \
  X(libusb_open) \
  X(libusb_close) \
  X(libusb_get_active_config_descriptor) \
  X(libusb_get_config_descriptor) \
  X(libusb_free_config_descriptor) \
  X(libusb_claim_interface) \
  X(libusb_release_interface) \
  X(libusb_set_interface_alt_setting) \
  X(libusb_alloc_transfer) \
  X(libusb_free_transfer) \
  X(libusb_submit_transfer) \
  X(libusb_cancel_transfer) \
  X(libusb_handle_events_timeout_completed) \
  X(libusb_get_version) \
  X(libusb_error_name) \
  X(libusb_interrupt_event_handler)
// clang-format on
struct UsbApi {
  Module module;
#define DECLARE(name) decltype(&::name) name = nullptr;
  USB_APIS(DECLARE)
#undef DECLARE
  explicit UsbApi(const fs::path &p) : module(p / L"libusb-1.0.dll") {
#define LOAD(name) name = module.get<decltype(name)>(#name);
    USB_APIS(LOAD)
#undef LOAD
    const auto *v = libusb_get_version();
    require(v->major == 1 && v->minor == 0 && v->micro == 30, "Verified libusb 1.0.30 is required");
  }
};
struct UsbDevices {
  UsbApi &api;
  libusb_device **items = nullptr;
  ssize_t count;
  UsbDevices(UsbApi &a, libusb_context *context)
      : api(a), count(a.libusb_get_device_list(context, &items)) {
    require(count >= 0, "USB enumeration failed");
  }
  ~UsbDevices() {
    if (items)
      api.libusb_free_device_list(items, 1);
  }
};
inline json controller_inventory(const fs::path &assets) {
  UsbApi api(assets);
  libusb_context *context = nullptr;
  require(api.libusb_init(&context) == 0, "USB inventory unavailable");
  json result = json::array();
  try {
    UsbDevices devices(api, context);
    for (ssize_t i = 0; i < devices.count; ++i) {
      libusb_device_descriptor d{};
      if (api.libusb_get_device_descriptor(devices.items[i], &d) != 0)
        continue;
      libusb_config_descriptor *config = nullptr;
      if (api.libusb_get_config_descriptor(devices.items[i], 0, &config) != 0 || !config)
        continue;
      bool hci = false, voice = false;
      for (int n = 0; n < config->bNumInterfaces; ++n)
        for (int a = 0; a < config->interface[n].num_altsetting; ++a) {
          const auto &s = config->interface[n].altsetting[a];
          hci |=
              s.bInterfaceClass == 0xe0 && s.bInterfaceSubClass == 1 && s.bInterfaceProtocol == 1;
          bool in = false, out = false;
          for (int e = 0; e < s.bNumEndpoints; ++e)
            if ((s.endpoint[e].bmAttributes & 3) == LIBUSB_TRANSFER_TYPE_ISOCHRONOUS) {
              in |= (s.endpoint[e].bEndpointAddress & 0x80) != 0;
              out |= (s.endpoint[e].bEndpointAddress & 0x80) == 0;
            }
          voice |= in && out;
        }
      api.libusb_free_config_descriptor(config);
      if (!hci)
        continue;
      auto instance = windows_instance(api.libusb_get_session_data(devices.items[i]));
      if (instance.empty())
        continue;
      auto id = physical_id(instance);
      char label[64];
      std::snprintf(label, sizeof(label), "USB Bluetooth %04X:%04X", d.idVendor, d.idProduct);
      result.push_back({{"transport", "usb-id:" + id},
                        {"adapter_id", id},
                        {"name", label},
                        {"vid", d.idVendor},
                        {"pid", d.idProduct},
                        {"revision", d.bcdDevice},
                        {"voice_transport", voice}});
    }
  } catch (...) {
    api.libusb_exit(context);
    throw;
  }
  api.libusb_exit(context);
  return result;
}
class Usb {
  struct Setting {
    int interface = -1, alt = 0;
    uint8_t in = 0, out = 0;
    uint16_t in_size = 0, out_size = 0;
  };
  struct Transfer {
    Usb *owner = nullptr;
    libusb_transfer *native = nullptr;
    std::vector<uint8_t> buffer;
    uint8_t type = 0;
    uint32_t capacity = 0;
    bool submitted = false, iso = false;
  };
  struct Splitter {
    uint32_t size = 0, wanted = 0;
    std::array<uint8_t, 65540> bytes{};
  };
  std::mutex event_mutex;
  UsbApi api;
  fs::path assets;
  bool driver_prepare = false;
  HciTarget &target;
  HciDispatch dispatch;
  libusb_context *context = nullptr;
  libusb_device_handle *device = nullptr;
  Setting acl;
  std::map<int, Setting> sco_settings;
  int sco_interface = -1, current_alt = 0, wanted_alt = 0;
  bool acl_claimed = false, sco_claimed = false;
  struct ScoLink {
    uint16_t handle = 0xffff;
    uint8_t mode = 0;
  };
  std::array<ScoLink, 3> sco_links;
  bool sixteen = true;
  std::vector<std::unique_ptr<Transfer>> normal, iso;
  Transfer *control_out = nullptr;
  std::array<Splitter, 5> splitters;
  Spsc<SmallPacket, 256> controls;
  Spsc<SmallPacket, 128> writes;
  std::thread worker, pacer;
  std::atomic<bool> pace_voice{false}, stop_pacer{false};
  Handle pace_wake{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
  std::atomic<bool> stopping{false}, alive{false};
  Handle ready{CreateEventW(nullptr, TRUE, FALSE, nullptr)},
      done{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
  Handle local_lease, global_lease, legacy_local_lease, legacy_global_lease;
  std::string requested_id, selected_instance, selected_id;
  ControllerCapabilities capabilities;
  std::function<std::string(unsigned long)> resolve_instance = windows_instance;
  std::string init_error;
  std::string init_code, init_profile;
  mutable std::mutex init_mutex;
  std::string firmware_state = "opening";
  unsigned firmware_progress = 0;
  std::unique_ptr<ControllerBootstrap> bootstrap;
  std::atomic<bool> initializing{false};
  uint64_t sco_completed = 0;
  uint64_t tx_next = 0;
  uint16_t vid = 0, pid = 0;
  unsigned index = 0;
  std::atomic<uint64_t> rx_packets{0}, tx_packets{0}, iso_errors{0}, control_overflows{0},
      deadline_misses{0};
  std::atomic<int> last_submit_error{0}, last_transfer_status{LIBUSB_TRANSFER_COMPLETED};
  void check(int result, const char *stage) {
    if (result < 0)
      throw std::runtime_error(std::string(stage) + ": " + api.libusb_error_name(result) + " (" +
                               std::to_string(result) + ")");
  }
  static std::wstring lease_suffix(uint16_t v, uint16_t p, unsigned i) {
    char canonical[80];
    std::snprintf(canonical, sizeof(canonical), "usb:%04x:%04x#%u", v, p, i);
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD count = 0, object_size = 0;
    require(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0,
            "HCI lease hash failed");
    BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&object_size),
                      sizeof(object_size), &count, 0);
    std::vector<uint8_t> object(object_size);
    std::array<uint8_t, 32> digest{};
    auto a = BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0);
    auto b = a >= 0 ? BCryptHashData(hash, reinterpret_cast<PUCHAR>(canonical),
                                     static_cast<ULONG>(std::strlen(canonical)), 0)
                    : a;
    auto c = b >= 0 ? BCryptFinishHash(hash, digest.data(), 32, 0) : b;
    if (hash)
      BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    require(c >= 0, "HCI lease hash failed");
    return wide(hex(digest.data(), 12));
  }
  void acquire_lease() {
    auto check_driver_operation = [this] {
      if (driver_prepare)
        return;
      SetLastError(0);
      Handle operation(OpenMutexW(SYNCHRONIZE, FALSE, L"Global\\BlueHead.DriverOperation"));
      require(!operation && GetLastError() == ERROR_FILE_NOT_FOUND,
              "A Bluetooth driver operation is in progress; wait for it to complete");
    };
    check_driver_operation();
    auto suffix = selected_id.empty() ? lease_suffix(vid, pid, index) : wide(selected_id);
    for (auto *item : {&local_lease, &global_lease}) {
      auto name = (item == &local_lease ? L"Local\\BlueHead-Controller-"
                                        : L"Global\\BlueHead-Controller-") +
                  suffix;
      SetLastError(0);
      item->reset(CreateMutexW(nullptr, FALSE, name.c_str()));
      DWORD error = GetLastError();
      require(static_cast<bool>(*item) && error != ERROR_ALREADY_EXISTS,
              "Bluetooth adapter already has an HCI owner");
    }
    // During migration, also exclude a previous release using an ordinal lease.
    auto legacy = lease_suffix(vid, pid, index);
    for (auto *item : {&legacy_local_lease, &legacy_global_lease}) {
      auto name =
          (item == &legacy_local_lease ? L"Local\\BlueHead-HCI-" : L"Global\\BlueHead-HCI-") +
          legacy;
      SetLastError(0);
      item->reset(CreateMutexW(nullptr, FALSE, name.c_str()));
      DWORD error = GetLastError();
      require(static_cast<bool>(*item) && error != ERROR_ALREADY_EXISTS,
              "Bluetooth adapter already has an HCI owner");
    }
    check_driver_operation();
  }
  void inspect() {
    UsbDevices devices(api, context);
    auto list = devices.items;
    auto count = devices.count;
    unsigned selected = 0, matches = 0;
    libusb_device *chosen = nullptr;
    libusb_device_descriptor descriptor{};
    for (ssize_t i = 0; i < count; i++) {
      libusb_device_descriptor d{};
      if (api.libusb_get_device_descriptor(list[i], &d) == 0) {
        auto instance = resolve_instance(api.libusb_get_session_data(list[i]));
        bool same = requested_id.empty()
                        ? d.idVendor == vid && d.idProduct == pid
                        : !instance.empty() && physical_id(instance) == requested_id;
        if (!same)
          continue;
        ++matches;
        if (!chosen && (requested_id.empty() ? selected++ == index : true)) {
          chosen = list[i];
          descriptor = d;
          selected_instance = instance;
        }
      }
    }
    if (!chosen) {
      throw std::runtime_error("Selected USB Bluetooth adapter is unavailable");
    }
    if (matches != 1) {
      throw std::runtime_error(
          "Controller identity: ambiguous physical adapter; select the device again");
    }
    vid = descriptor.idVendor;
    pid = descriptor.idProduct;
    selected_id = selected_instance.empty() ? "" : physical_id(selected_instance);
    index = 0;
    for (ssize_t i = 0; i < count && list[i] != chosen; ++i) {
      libusb_device_descriptor d{};
      if (api.libusb_get_device_descriptor(list[i], &d) == 0 && d.idVendor == vid &&
          d.idProduct == pid)
        ++index;
    }
    acquire_lease();
    int result = api.libusb_open(chosen, &device);
    libusb_config_descriptor *config = nullptr;
    if (result == 0) {
      result = api.libusb_get_active_config_descriptor(chosen, &config);
      if (result < 0)
        result = api.libusb_get_config_descriptor(chosen, 0, &config);
    }
    check(result, device ? "USB configuration unavailable" : "Cannot open WinUSB adapter");
    require(config, "USB configuration unavailable");
    for (int i = 0; i < config->bNumInterfaces; i++)
      for (int j = 0; j < config->interface[i].num_altsetting; j++) {
        const auto &s = config->interface[i].altsetting[j];
        Setting candidate;
        candidate.interface = s.bInterfaceNumber;
        candidate.alt = s.bAlternateSetting;
        uint8_t interrupt = 0, bulk_in = 0, bulk_out = 0;
        for (int e = 0; e < s.bNumEndpoints; e++) {
          const auto &ep = s.endpoint[e];
          int type = ep.bmAttributes & 3;
          bool in = (ep.bEndpointAddress & 0x80) != 0;
          if (type == LIBUSB_TRANSFER_TYPE_INTERRUPT && in)
            interrupt = ep.bEndpointAddress;
          if (type == LIBUSB_TRANSFER_TYPE_BULK) {
            if (in)
              bulk_in = ep.bEndpointAddress;
            else
              bulk_out = ep.bEndpointAddress;
          }
          if (type == LIBUSB_TRANSFER_TYPE_ISOCHRONOUS && (ep.wMaxPacketSize & 0x7ff)) {
            if (in && (ep.wMaxPacketSize & 0x7ff) > candidate.in_size) {
              candidate.in = ep.bEndpointAddress;
              candidate.in_size = ep.wMaxPacketSize & 0x7ff;
            }
            if (!in && (ep.wMaxPacketSize & 0x7ff) > candidate.out_size) {
              candidate.out = ep.bEndpointAddress;
              candidate.out_size = ep.wMaxPacketSize & 0x7ff;
            }
          }
        }
        if (interrupt && bulk_in && bulk_out &&
            (s.bInterfaceClass == 0xe0 || descriptor.bDeviceClass == 0xe0)) {
          acl = candidate;
          acl.in = bulk_in;
          acl.out = bulk_out;
          acl.in_size = interrupt;
        }
        if (candidate.in && candidate.out) {
          sco_settings[candidate.alt] = candidate;
          sco_interface = candidate.interface;
        }
      }
    api.libusb_free_config_descriptor(config);
    require(acl.interface >= 0, "No Bluetooth HCI USB interface");
    {
      std::lock_guard<std::mutex> lock(init_mutex);
      capabilities.media_transport = true;
      capabilities.voice_transport = sco_interface >= 0;
    }
    check(api.libusb_claim_interface(device, acl.interface), "Cannot claim HCI USB interface");
    acl_claimed = true;
    if (sco_interface >= 0) {
      check(api.libusb_claim_interface(device, sco_interface), "Cannot claim voice USB interface");
      sco_claimed = true;
      check(api.libusb_set_interface_alt_setting(device, sco_interface, 0),
            "Cannot reset voice alternate setting");
    }
  }
  Transfer *make(std::vector<std::unique_ptr<Transfer>> &group, uint8_t type, uint8_t endpoint,
                 uint32_t capacity, int iso_count = 0) {
    auto t = std::make_unique<Transfer>();
    t->owner = this;
    t->type = type;
    t->iso = iso_count != 0;
    t->capacity = capacity;
    t->buffer.resize(capacity * static_cast<size_t>(iso_count ? iso_count : 1));
    t->native = api.libusb_alloc_transfer(iso_count);
    require(t->native, "USB transfer allocation failed");
    auto *v = t->native;
    v->dev_handle = device;
    v->endpoint = endpoint;
    v->type = t->iso ? LIBUSB_TRANSFER_TYPE_ISOCHRONOUS
                     : (type == 4 ? LIBUSB_TRANSFER_TYPE_INTERRUPT : LIBUSB_TRANSFER_TYPE_BULK);
    v->buffer = t->buffer.data();
    v->length = static_cast<int>(t->buffer.size());
    // Idle IN reads must remain pending. HCI command/ACL OUT has a deadline:
    // an unresponsive controller must not hold the entire control queue forever.
    v->timeout = type == 0 && !iso_count ? 2000 : 0;
    v->callback = complete;
    v->user_data = t.get();
    v->num_iso_packets = iso_count;
    for (int i = 0; i < iso_count; i++)
      v->iso_packet_desc[i].length = capacity;
    auto *result = t.get();
    group.push_back(std::move(t));
    return result;
  }
  void submit(Transfer *t) {
    int result = api.libusb_submit_transfer(t->native);
    if (result < 0) {
      last_submit_error = result;
      stopping = true;
      return;
    }
    t->submitted = true;
  }
  void forward(uint8_t type, const uint8_t *data, uint32_t size) {
    if (size + 1 > 4096) {
      control_overflows++;
      stopping = true;
      return;
    }
    SmallPacket p;
    p.size = size + 1;
    p.data[0] = type;
    std::memcpy(p.data.data() + 1, data, size);
    if (!controls.push(p)) {
      control_overflows++;
      stopping = true;
    }
  }
  void event(const uint8_t *p, uint32_t n) {
    if (n >= 19 && p[0] == 0x2c && p[2] == 0) {
      uint16_t handle = le16(p + 3) & 0xfff;
      auto found = std::find_if(sco_links.begin(), sco_links.end(),
                                [handle](const auto &v) { return v.handle == handle; });
      if (found == sco_links.end())
        found = std::find_if(sco_links.begin(), sco_links.end(),
                             [](const auto &v) { return v.handle == 0xffff; });
      if (found != sco_links.end())
        *found = {handle, p[18]};
      else {
        control_overflows++;
        stopping = true;
      }
    }
    if (n >= 6 && p[0] == 5 && p[2] == 0) {
      uint16_t handle = le16(p + 3) & 0xfff;
      for (auto &v : sco_links)
        if (v.handle == handle)
          v.handle = 0xffff;
      dispatch.reset(handle);
      target.disconnected(handle);
    }
    wanted_alt = 0;
    size_t links = std::count_if(sco_links.begin(), sco_links.end(),
                                 [](const auto &v) { return v.handle != 0xffff; });
    if (links && !sco_settings.empty()) {
      bool transparent = std::any_of(sco_links.begin(), sco_links.end(), [](const auto &v) {
        return v.handle != 0xffff && v.mode == 3;
      });
      int candidate = transparent ? 6
                                  : (sixteen ? (links == 1   ? 2
                                                : links == 2 ? 4
                                                             : 5)
                                             : static_cast<int>(links));
      if (sco_settings.count(candidate))
        wanted_alt = candidate;
      else if (transparent && sco_settings.count(1))
        wanted_alt = 1;
      else
        wanted_alt = sco_settings.rbegin()->first;
    }
  }
  void packet(uint8_t type, const uint8_t *p, uint32_t n) {
    rx_packets++;
    if (bootstrap) {
      if (type == 4)
        bootstrap->event(p, n);
      // No ACL or voice packet is valid for the host until bootstrap commits.
      return;
    }
    if (type == 2 && dispatch.acl(p, n))
      return;
    if (type == 3) {
      if (n < 3 || p[2] != n - 3) {
        iso_errors++;
        return;
      }
      if (target.voice_packet(le16(p) & 0xfff, (p[1] >> 4) & 3, p + 3, n - 3))
        return;
    }
    if (type == 4) {
      event(p, n);
      std::array<uint8_t, 257> filtered{};
      uint32_t length = 0;
      if (filter_completions(
              p, n, filtered, length,
              [this](uint16_t handle) {
                return std::any_of(sco_links.begin(), sco_links.end(),
                                   [handle](const auto &v) { return v.handle == handle; });
              },
              sco_completed)) {
        if (length)
          forward(type, filtered.data(), length);
        return;
      }
    }
    forward(type, p, n);
  }
  void feed(uint8_t type, const uint8_t *p, uint32_t n) {
    auto &s = splitters[type];
    bool boot_event = type == 2 && bootstrap && bootstrap->bulk_events;
    uint32_t header = boot_event ? 2 : type == 2 ? 4 : type == 3 ? 3 : 2;
    while (n) {
      uint32_t want = s.size < header ? header : s.wanted;
      if (want < s.size || want > s.bytes.size()) {
        s.size = s.wanted = 0;
        iso_errors++;
        return;
      }
      uint32_t take = std::min(n, want - s.size);
      std::memcpy(s.bytes.data() + s.size, p, take);
      s.size += take;
      p += take;
      n -= take;
      if (s.size == header && !s.wanted)
        s.wanted = header + (type == 2 && !boot_event ? le16(s.bytes.data() + 2)
                                                      : s.bytes[type == 3 ? 2 : 1]);
      if (s.wanted && s.size == s.wanted) {
        packet(boot_event ? 4 : type, s.bytes.data(), s.size);
        s.size = s.wanted = 0;
      }
    }
  }
  static void LIBUSB_CALL complete(libusb_transfer *v) noexcept {
    try {
      complete_checked(v);
    } catch (...) {
      auto *t = static_cast<Transfer *>(v->user_data);
      t->submitted = false;
      t->owner->stopping = true;
    }
  }
  static void complete_checked(libusb_transfer *v) {
    auto *t = static_cast<Transfer *>(v->user_data);
    auto *u = t->owner;
    t->submitted = false;
    if (u->stopping || v->status == LIBUSB_TRANSFER_CANCELLED)
      return;
    if (v->status != LIBUSB_TRANSFER_COMPLETED)
      u->last_transfer_status = v->status;
    if (t == u->control_out || t->type == 0) {
      if (v->status != LIBUSB_TRANSFER_COMPLETED) {
        u->iso_errors++;
        if (t == u->control_out)
          u->stopping = true;
      }
      return;
    }
    if (v->status == LIBUSB_TRANSFER_NO_DEVICE) {
      u->stopping = true;
      return;
    }
    if (v->status == LIBUSB_TRANSFER_COMPLETED) {
      if (t->iso) {
        for (int i = 0; i < v->num_iso_packets; i++) {
          const auto &d = v->iso_packet_desc[i];
          v->iso_packet_desc[i].length = t->capacity;
          uint32_t offset = static_cast<uint32_t>(i) * t->capacity;
          if (d.status != LIBUSB_TRANSFER_COMPLETED || d.actual_length > t->capacity ||
              offset + d.actual_length > t->buffer.size()) {
            u->iso_errors++;
            u->splitters[3].size = u->splitters[3].wanted = 0;
            continue;
          }
          if (d.actual_length)
            u->feed(3, t->buffer.data() + offset, d.actual_length);
          v->iso_packet_desc[i].length = t->capacity;
        }
      } else if (v->actual_length >= 0 && static_cast<size_t>(v->actual_length) <= t->buffer.size())
        u->feed(t->type, t->buffer.data(), v->actual_length);
      else
        u->iso_errors++;
    } else {
      u->iso_errors++;
      if (t->iso)
        u->splitters[3].size = u->splitters[3].wanted = 0;
      else
        u->stopping = true; // A broken reliable HCI stream has no safe byte resync.
    }
    if (!u->stopping && (!t->iso || u->wanted_alt == u->current_alt))
      u->submit(t);
  }
  void cancel(std::vector<std::unique_ptr<Transfer>> &group) {
    for (auto &t : group)
      if (t->submitted)
        api.libusb_cancel_transfer(t->native);
  }
  bool pending(const std::vector<std::unique_ptr<Transfer>> &group) {
    for (auto &t : group)
      if (t->submitted)
        return true;
    return false;
  }
  void events() {
    timeval timeout{0, 100000};
    int result = api.libusb_handle_events_timeout_completed(context, &timeout, nullptr);
    if (result < 0 && result != LIBUSB_ERROR_INTERRUPTED) {
      last_submit_error = result;
      stopping = true;
    }
  }
  void release(std::vector<std::unique_ptr<Transfer>> &group) {
    for (auto &t : group) {
      require(!t->submitted, "Cannot free an active USB transfer");
      api.libusb_free_transfer(t->native);
    }
    group.clear();
  }
  void change_alt() {
    cancel(iso);
    auto deadline = seconds() + 3;
    while (pending(iso)) {
      events();
      if (seconds() > deadline)
        TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
    }
    release(iso);
    splitters[3].size = splitters[3].wanted = 0;
    tx_next = 0;
    require(sco_interface >= 0 &&
                api.libusb_set_interface_alt_setting(device, sco_interface, wanted_alt) == 0,
            "Voice alternate setting failed");
    current_alt = wanted_alt;
    pace_voice = current_alt != 0;
    SetEvent(pace_wake);
    if (current_alt) {
      const auto &s = sco_settings.at(current_alt);
      require(s.in_size <= 1024 && s.out_size <= 1024, "Unsupported USB ISO capacity");
      for (int i = 0; i < 2; i++)
        submit(make(iso, 3, s.in, s.in_size, 16));
      int count = current_alt == 6 ? 1 : 3;
      for (int i = 0; i < 4; i++)
        make(iso, 0, s.out, s.out_size, count);
    }
  }
  void pump_send() {
    if (control_out && !control_out->submitted) {
      SmallPacket p;
      if (writes.pop(p)) {
        auto *t = control_out;
        auto *v = t->native;
        uint8_t type = p.data[0];
        if (type == 1) {
          if (p.size < 4 || p.data[3] != p.size - 4) {
            iso_errors++;
            return;
          }
          if (le16(p.data.data() + 1) == 0x0c26 && p.size >= 6)
            sixteen = (le16(p.data.data() + 4) & 0x20) != 0;
          libusb_control_setup setup{};
          setup.bmRequestType = 0x20 | (acl.interface ? 1 : 0);
          setup.bRequest = 0;
          setup.wIndex = static_cast<uint16_t>(acl.interface);
          setup.wLength = static_cast<uint16_t>(p.size - 1);
          std::memcpy(t->buffer.data(), &setup, sizeof(setup));
          std::memcpy(t->buffer.data() + sizeof(setup), p.data.data() + 1, p.size - 1);
          v->endpoint = 0;
          v->type = LIBUSB_TRANSFER_TYPE_CONTROL;
          v->length = static_cast<int>(sizeof(setup) + p.size - 1);
        } else if (type == 2) {
          std::memcpy(t->buffer.data(), p.data.data() + 1, p.size - 1);
          v->endpoint = acl.out;
          v->type = LIBUSB_TRANSFER_TYPE_BULK;
          v->length = static_cast<int>(p.size - 1);
        } else
          return;
        tx_packets++;
        submit(t);
      }
    }
    if (!current_alt)
      return;
    auto now = ticks();
    if (tx_next && now < tx_next)
      return;
    auto bytes_per_second = target.voice_byte_rate();
    if (!bytes_per_second) {
      tx_next = 0;
      return;
    }
    const auto &setting = sco_settings.at(current_alt);
    uint32_t frames = current_alt == 6 ? 1 : 3;
    uint32_t size = frames * setting.out_size - 3;
    if (!size || size > 255) {
      iso_errors++;
      return;
    }
    for (auto &holder : iso) {
      auto *t = holder.get();
      if (t->type != 0 || t->submitted)
        continue;
      uint16_t handle = 0;
      uint32_t got = target.voice_output(handle, t->buffer.data() + 3, size);
      if (got != size)
        return;
      put16(t->buffer.data(), handle);
      t->buffer[2] = static_cast<uint8_t>(size);
      t->native->length = static_cast<int>(size + 3);
      for (uint32_t j = 0; j < frames; j++)
        t->native->iso_packet_desc[j].length = setting.out_size;
      uint64_t interval = std::max<uint64_t>(1, frequency() * size / bytes_per_second);
      if (tx_next && now > tx_next + interval * 2) {
        deadline_misses++;
        tx_next = now;
      }
      tx_next = (tx_next ? tx_next : now) + interval;
      tx_packets++;
      submit(t);
      return;
    }
  }
  void bootstrap_pump() {
    require(!stopping, "Controller initialization: cancelled or USB transport failed");
    events();
    require(!stopping, "Controller initialization: cancelled or USB transport failed");
  }
  void bootstrap_write(uint16_t opcode, const std::vector<uint8_t> &data, bool bulk) {
    require(data.size() <= 255, "Controller initialization: command exceeds HCI capacity");
    auto deadline = seconds() + 2.5;
    while (control_out->submitted) {
      require(seconds() < deadline, "Controller initialization: USB write completion timeout");
      bootstrap_pump();
    }
    auto *v = control_out->native;
    size_t offset = bulk ? 0 : sizeof(libusb_control_setup);
    if (!bulk) {
      libusb_control_setup setup{};
      setup.bmRequestType = 0x20 | (acl.interface ? 1 : 0);
      setup.wIndex = uint16_t(acl.interface);
      setup.wLength = uint16_t(3 + data.size());
      std::memcpy(control_out->buffer.data(), &setup, sizeof(setup));
    }
    auto *p = control_out->buffer.data() + offset;
    put16(p, opcode);
    p[2] = uint8_t(data.size());
    std::copy(data.begin(), data.end(), p + 3);
    v->endpoint = bulk ? acl.out : 0;
    v->type = bulk ? LIBUSB_TRANSFER_TYPE_BULK : LIBUSB_TRANSFER_TYPE_CONTROL;
    v->length = int(offset + 3 + data.size());
    tx_packets++;
    submit(control_out);
    check(last_submit_error, "Cannot submit controller initialization command");
  }
  void initialize_firmware() {
    bootstrap =
        controller_bootstrap(vid, pid,
                             BootstrapIO{[this](uint16_t op, const std::vector<uint8_t> &data,
                                                bool bulk) { bootstrap_write(op, data, bulk); },
                                         [this] { bootstrap_pump(); },
                                         [this](const char *state, unsigned progress) {
                                           std::lock_guard<std::mutex> lock(init_mutex);
                                           firmware_state = state;
                                           firmware_progress = progress;
                                           capabilities.firmware = state;
                                         }});
    {
      std::lock_guard<std::mutex> lock(init_mutex);
      capabilities.backend = bootstrap->name();
    }
    bootstrap->run(assets);
    {
      std::lock_guard<std::mutex> lock(init_mutex);
      capabilities.hci = bootstrap->result();
      capabilities.backend = bootstrap->name();
      capabilities.voice_transport =
          capabilities.voice_transport && capabilities.hci.value("voice_capable", false);
    }
    auto deadline = seconds() + 2.5;
    while (control_out->submitted) {
      require(seconds() < deadline, "Controller initialization: final USB write completion timeout");
      bootstrap_pump();
    }
    bootstrap.reset();
    // Bulk changes from event framing to ACL framing after the boot event.
    splitters[2].size = splitters[2].wanted = 0;
  }
  void run() {
    Priority priority;
    try {
      require(api.libusb_init(&context) == 0, "libusb initialization failed");
      inspect();
      pacer = std::thread([this] {
        Priority priority;
        Timer timer;
        while (!stop_pacer) {
          if (!pace_voice) {
            WaitForSingleObject(pace_wake, INFINITE);
            continue;
          }
          timer.wait(ticks() + frequency() / 1000, pace_wake);
          if (!stop_pacer && pace_voice)
            api.libusb_interrupt_event_handler(context);
        }
      });
      submit(make(normal, 4, static_cast<uint8_t>(acl.in_size), 4096));
      submit(make(normal, 2, acl.in, 4096));
      check(last_submit_error, "Cannot submit initial HCI USB reads");
      control_out = make(normal, 0, acl.out, 4104);
      initialize_firmware();
      alive = true;
      initializing = false;
      SetEvent(ready);
      while (!stopping) {
        events();
        if (stopping)
          break;
        if (wanted_alt != current_alt)
          change_alt();
        pump_send();
      }
    } catch (const std::exception &e) {
      {
        std::lock_guard<std::mutex> lock(init_mutex);
        init_error = e.what();
        if (auto firmware_error = dynamic_cast<const firmware::Error *>(&e)) {
          init_code = firmware_error->code;
          init_profile = firmware_error->profile;
        }
        firmware_state = "failed";
      }
      stopping = true;
      SetEvent(ready);
    }
    alive = false;
    initializing = false;
    SetEvent(ready);
    target.transport_lost();
    stop_pacer = true;
    SetEvent(pace_wake);
    if (pacer.joinable())
      pacer.join();
    cancel(normal);
    cancel(iso);
    auto deadline = seconds() + 3;
    while (context && (pending(normal) || pending(iso))) {
      events();
      if (seconds() > deadline)
        TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
    }
    release(normal);
    release(iso);
    control_out = nullptr;
    if (device) {
      if (sco_claimed) {
        api.libusb_set_interface_alt_setting(device, sco_interface, 0);
        api.libusb_release_interface(device, sco_interface);
      }
      if (acl_claimed)
        api.libusb_release_interface(device, acl.interface);
      api.libusb_close(device);
      device = nullptr;
    }
    if (context) {
      std::lock_guard<std::mutex> lock(event_mutex);
      api.libusb_exit(context);
      context = nullptr;
    }
    local_lease.reset();
    global_lease.reset();
    legacy_local_lease.reset();
    legacy_global_lease.reset();
    SetEvent(done);
  }

public:
  Usb(const fs::path &p, HciTarget &t, bool preparing_driver = false,
      std::function<std::string(unsigned long)> resolver = windows_instance)
      : api(p), assets(p), driver_prepare(preparing_driver), target(t), dispatch(t),
        resolve_instance(std::move(resolver)) {}
  ~Usb() { close(); }
  void start(uint16_t v, uint16_t p, unsigned i) {
    require(!worker.joinable(), "USB owner already started");
    vid = v;
    pid = p;
    index = i;
    initializing = true;
    worker = std::thread([this] { run(); });
  }
  void start_id(const std::string &id) {
    require(valid_adapter_id(id), "Controller identity: invalid adapter identifier");
    requested_id = id;
    start(0, 0, 0);
  }
  void open(uint16_t v, uint16_t p, unsigned i) {
    start(v, p, i);
    if (WaitForSingleObject(ready, 70000) != WAIT_OBJECT_0) {
      close();
      throw std::runtime_error("USB initialization timeout");
    }
    std::string error;
    {
      std::lock_guard<std::mutex> lock(init_mutex);
      error = init_error;
    }
    if (!error.empty()) {
      close();
      throw std::runtime_error(error);
    }
  }
  void close() {
    stopping = true;
    {
      std::lock_guard<std::mutex> lock(event_mutex);
      if (context)
        api.libusb_interrupt_event_handler(context);
    }
    if (worker.joinable()) {
      if (WaitForSingleObject(done, 5000) != WAIT_OBJECT_0)
        TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
      worker.join();
    }
  }
  bool send(const uint8_t *p, uint32_t n) {
    if (!alive || !p || n < 1 || n > 4096)
      return false;
    SmallPacket packet;
    packet.size = n;
    std::memcpy(packet.data.data(), p, n);
    bool queued = writes.push(packet);
    if (queued) {
      std::lock_guard<std::mutex> lock(event_mutex);
      if (context)
        api.libusb_interrupt_event_handler(context);
    }
    return queued;
  }
  bool pop(SmallPacket &p) { return controls.pop(p); }
  bool active() const { return alive.load(); }
  json startup() const {
    std::lock_guard<std::mutex> lock(init_mutex);
    return {{"starting", initializing.load()},
            {"firmware_state", firmware_state},
            {"firmware_progress", firmware_progress},
            {"capabilities", capabilities.snapshot()},
            {"firmware_error", init_code},
            {"firmware_profile", init_profile},
            {"error", init_error}};
  }
  const char *terminal_reason() const {
    if (control_overflows.load())
      return "control_backpressure";
    if (last_transfer_status.load() == LIBUSB_TRANSFER_NO_DEVICE)
      return "device_removed";
    if (last_submit_error.load() < 0)
      return "usb_submit_failed";
    if (last_transfer_status.load() != LIBUSB_TRANSFER_COMPLETED)
      return "usb_transfer_failed";
    return "stopped";
  }
  json snapshot() const {
    auto result = startup();
    result.update({{"active", active()},
                   {"rx_packets", rx_packets.load()},
                   {"tx_packets", tx_packets.load()},
                   {"iso_errors", iso_errors.load()},
                   {"control_overflows", control_overflows.load()},
                   {"tx_deadline_misses", deadline_misses.load()},
                   {"last_submit_error", last_submit_error.load()},
                   {"last_transfer_status", last_transfer_status.load()},
                   {"terminal_reason", active() ? "" : terminal_reason()},
                   {"single_owner", true}});
    return result;
  }
};
} // namespace bh
