/* Single native libusb/HCI owner. Apache-2.0. No Python callbacks or PCM IPC.
 */
#pragma once
#include "command_deadline.hpp"
#include "runtime_common.hpp"
#include "runtime_hci_events.hpp"
#include "hci_dispatch.hpp"
#include "hci_links.hpp"
#include "hci_framing.hpp"
#include "le_capabilities.hpp"
#include "iso_sender.hpp"
#include "controller_health.hpp"
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
  std::mutex event_mutex;
  UsbApi api;
  fs::path assets;
  bool driver_prepare = false;
  HciTarget &target;
  HciDispatch dispatch;
  libusb_context *context = nullptr;
  libusb_device_handle *device = nullptr;
  Setting acl, serialized;
  bool iso_requested = false, serialized_mode = false, retiring_reads = false;
  LeCapabilities le_capabilities;
  IsoSender iso_sender;
  CommandDeadline command_deadline;
  std::atomic<bool> command_failed{false};
  uint16_t sync_handle=0xffff;
  uint32_t sync_epoch=0;
  std::array<Transfer *,4> iso_bulk_out{};
  std::atomic<bool> iso_flow_failed{false};
  Transfer *serialized_voice_out = nullptr;
  std::map<int, Setting> sco_settings;
  int sco_interface = -1, current_alt = 0, wanted_alt = 0;
  bool acl_claimed = false, sco_claimed = false;
  HciLinks link_lifetime;
  bool sixteen = true;
  std::vector<std::unique_ptr<Transfer>> normal, iso;
  Transfer *control_out = nullptr;
  std::array<HciPacketReader, 6> readers;
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
  uint32_t synchronous_limit=0;
  uint64_t tx_next = 0;
  uint16_t vid = 0, pid = 0;
  unsigned index = 0;
  std::atomic<uint64_t> rx_packets{0}, tx_packets{0}, iso_errors{0}, control_overflows{0},
      deadline_misses{0};
  std::atomic<int> last_submit_error{0}, last_transfer_status{LIBUSB_TRANSFER_COMPLETED};
  std::atomic<bool> startup_complete{false}, controller_failed{false}, controller_restarted{false}, callback_failed{false};
  ControllerEvidence controller_evidence;
  std::atomic<uint32_t> hardware_error{0}, last_event{0}, last_opcode{0};
  std::atomic<uint64_t> last_receive{0};
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
        if (candidate.alt == 0 && interrupt && bulk_in && bulk_out &&
            (s.bInterfaceClass == 0xe0 || descriptor.bDeviceClass == 0xe0)) {
          acl = candidate;
          acl.in = bulk_in;
          acl.out = bulk_out;
          acl.in_size = interrupt;
        }
        if (candidate.alt == 1 && s.bNumEndpoints == 2 && bulk_in && bulk_out &&
            !interrupt && !candidate.in && !candidate.out && s.bInterfaceSubClass == 1 &&
            s.bInterfaceProtocol == 1 && (s.bInterfaceClass == 0xe0 || descriptor.bDeviceClass == 0xe0)) {
          serialized = candidate; serialized.in = bulk_in; serialized.out = bulk_out;
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
    // Restore legacy before touching the SCO interface after a crashed host.
    check(api.libusb_set_interface_alt_setting(device, acl.interface, 0),
          "Cannot reset HCI alternate setting");
    if (serialized.interface != acl.interface) serialized = {};

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
    p.link_epoch=packet_link_identity(target,type,data,size);
    if (!controls.push(p)) {
      control_overflows++;
      stopping = true;
    }
  }
  void event(const uint8_t *p, uint32_t n) {
    if (!link_lifetime.event(p, n, [this](uint16_t handle) {
      iso_sender.disconnected(handle);
      dispatch.reset(handle);
      target.disconnected(handle);
    })) {
      control_overflows++;
      stopping = true;
    }
    // A TX Sync return has a THREE-octet offset (Core 7.8.96).
    // The command was captured by this same HCI owner, so a late result
    // cannot steer a replacement microphone after an ASE/CIS change.
    if(n>=5 && p[0]==0x0e && le16(p+3)==0x2061) {
      if(n==17 && p[1]==15 && !p[5] && le16(p+6)==sync_handle && sync_epoch) {
        uint32_t stamp=uint32_t(p[10])|uint32_t(p[11])<<8|uint32_t(p[12])<<16|uint32_t(p[13])<<24;
        uint32_t offset=uint32_t(p[14])|uint32_t(p[15])<<8|uint32_t(p[16])<<16;
        target.iso_clock_sample(sync_handle,sync_epoch,le16(p+8),stamp,offset,ticks());
      }
      sync_handle=0xffff;sync_epoch=0;
    }
    if(n==13 && p[0]==0x0e && p[1]==11 && le16(p+3)==0x1005 && !p[5])
      synchronous_limit=p[8];
    le_capabilities.observe(p, n);
    if (le_capabilities.mode()!=IsoTransport::None) {
      if (n>=6 && p[0]==0x0e && p[3]==3 && p[4]==0x0c && !p[5]) iso_sender={};
      if (le_capabilities.transmit()) iso_sender.configure(le_capabilities.packet_length(),le_capabilities.packet_count());
      if (!iso_sender.event(p,n)) { iso_flow_failed=true; stopping=true; }
    }

    if (serialized_mode) { wanted_alt = 0; return; }
    const auto &sco_links = link_lifetime.sco;
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
    last_receive = ticks();
    if (bootstrap) {
      if (type == 4)
        bootstrap->event(p, n);
      // No ACL or voice packet is valid for the host until bootstrap commits.
      return;
    }
    // Some controllers carry ISO on the legacy bulk endpoint. Route only
    // explicitly registered ISO handles; never guess by numeric handle range.
    if (type == 5 || (type == 2 && n >= 4 &&
        le_capabilities.mode() == IsoTransport::IntelLegacyBulk &&
        (link_lifetime.is_iso(le16(p) & 0xfff) || target.iso_handle(le16(p) & 0xfff)))) {
      if (!target.iso_packet(p, n)) iso_errors++;
      return; // Retired/unrouted ISO never crosses Python either.
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
      if(iso_requested) {
        auto state=command_deadline.observe(p,n);
        if(state==CommandDeadline::Event::Stale) return;
        if(state==CommandDeadline::Event::Invalid) {command_failed=true;stopping=true;return;}
      }
      if (n) last_event = p[0];
      auto signal = controller_signal(p, n, startup_complete.load(), capabilities.backend == "intel-tlv");
      if (signal != ControllerSignal::Normal) {
        controller_evidence.record(signal,p,n);
        controller_failed = signal == ControllerSignal::HardwareError;
        controller_restarted = signal == ControllerSignal::Restarted;
        if (signal == ControllerSignal::HardwareError) hardware_error = p[2];
        stopping = true;
        return;
      }
      event(p, n);
      std::array<uint8_t, 257> filtered{};
      uint32_t length = 0;
      if (filter_completions(
              p, n, filtered, length,
              [this](uint16_t handle) {
                return iso_sender.owns(handle) || link_lifetime.is_iso(handle) || std::any_of(link_lifetime.sco.begin(), link_lifetime.sco.end(),
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
    uint8_t actual = type == 2 && bootstrap && bootstrap->bulk_events ? 4 : type;
    if (actual >= readers.size() || !readers[type].feed(actual, p, n,
        [this](uint8_t kind, const uint8_t *bytes, uint32_t count) {
          packet(kind, bytes, count); return !stopping.load();
        })) {
      iso_errors++;
      stopping = true; // Reliable HCI framing cannot be resynchronized safely.
    }
  }
  static void LIBUSB_CALL complete(libusb_transfer *v) noexcept {
    try {
      complete_checked(v);
    } catch (...) {
      auto *t = static_cast<Transfer *>(v->user_data);
      t->submitted = false;
      t->owner->callback_failed = true;
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
        // A failed reliable ISO OUT has consumed a controller credit. Its
        // delivery is unknown: never guess a credit refund and continue.
        if (t == u->control_out || std::find(u->iso_bulk_out.begin(),u->iso_bulk_out.end(),t)!=u->iso_bulk_out.end())
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
            u->readers[3].reset();
            continue;
          }
          if (d.actual_length)
            u->feed(3, t->buffer.data() + offset, d.actual_length);
          v->iso_packet_desc[i].length = t->capacity;
        }
      } else if (v->actual_length >= 0 && static_cast<size_t>(v->actual_length) <= t->buffer.size())
        u->feed(t->type == 6 ? 0 : t->type, t->buffer.data(), v->actual_length);
      else
        u->iso_errors++;
    } else {
      u->iso_errors++;
      if (t->iso)
        u->readers[3].reset();
      else
        u->stopping = true; // A broken reliable HCI stream has no safe byte resync.
    }
    if (!u->stopping && !u->retiring_reads && (!t->iso || u->wanted_alt == u->current_alt))
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
    readers[3].reset();
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
  void select_iso_transport() {
    if (!iso_requested) return;
    const auto manufacturer = capabilities.hci.value("manufacturer", 0u);
    if (serialized.interface >= 0) {
      // Drain old reads before changing endpoint framing; never resubmit while retiring.
      retiring_reads = true; cancel(normal);
      auto deadline = seconds() + 3;
      while (pending(normal)) {
        events(); if (seconds() > deadline) TerminateProcess(GetCurrentProcess(), ERROR_TIMEOUT);
      }
      require(!stopping, "USB stopped during ISO transport selection");
      release(normal); control_out = nullptr;
      check(api.libusb_set_interface_alt_setting(device, acl.interface, 1),
            "Cannot enable HCI bulk serialization");
      acl.in = serialized.in; acl.out = serialized.out; serialized_mode = true;
      for (auto &reader : readers) reader.reset();
      retiring_reads = false;
      submit(make(normal, 6, acl.in, 4096)); // H4 on one serialized BULK IN stream.
      control_out = make(normal, 0, acl.out, 4104);
      serialized_voice_out = make(normal, 0, acl.out, 260);
      le_capabilities.select(IsoTransport::BulkSerialization);
    } else le_capabilities.select(iso_transport(false, vid, uint16_t(manufacturer)));
    if (le_capabilities.mode()!=IsoTransport::None)
      for (auto &out:iso_bulk_out) out=make(normal,0,acl.out,4104);
  }
  void pump_iso() {
    if (!le_capabilities.transmit()) return;
    for (auto *out:iso_bulk_out) {
      if (!out || out->submitted) continue;
      uint32_t offset=serialized_mode?1u:0u;
      auto n=iso_sender.output(target,out->buffer.data()+offset,uint32_t(out->buffer.size())-offset);
      if (!n) break;
      if (offset) out->buffer[0]=5;
      out->native->endpoint=acl.out;out->native->type=LIBUSB_TRANSFER_TYPE_BULK;out->native->length=int(n+offset);
      tx_packets++;submit(out);
    }
  }
  void pump_serialized_voice() {
    auto rate = target.voice_byte_rate();
    if (!serialized_voice_out || serialized_voice_out->submitted || !rate) { if (!rate) tx_next = 0; return; }
    auto now = ticks(); if (tx_next && now < tx_next) return;
    auto *t = serialized_voice_out; uint16_t handle = 0;
    // Controllers expose different synchronous packet capacities. Preserve
    // the encoded byte stream; splitting H2/CVSD across HCI packets is legal.
    uint32_t capacity=std::min(60u,synchronous_limit)&~1u;
    if(!capacity) return;
    uint32_t got = target.voice_output(handle, t->buffer.data()+4, capacity);
    if (got != capacity) return;
    t->buffer[0] = 3; put16(t->buffer.data()+1, handle); t->buffer[3] = uint8_t(got);
    t->native->endpoint = acl.out; t->native->type = LIBUSB_TRANSFER_TYPE_BULK;
    t->native->length = int(got+4);
    uint64_t interval = std::max<uint64_t>(1, frequency()*got/rate);
    if (tx_next && now > tx_next + 2*interval) { deadline_misses++; tx_next=now; }
    tx_next = (tx_next ? tx_next : now) + interval;
    tx_packets++; submit(t);
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
          if(iso_requested && !command_deadline.sent(le16(p.data.data()+1),ticks())) {
            command_failed=true;stopping=true;return;
          }
          if(le16(p.data.data()+1)==0x2061 && p.size==6) {
            uint16_t active;uint32_t epoch;
            sync_handle=0xffff;sync_epoch=0;
            if(target.iso_transmit_route(active,epoch) && active==le16(p.data.data()+4)) {sync_handle=active;sync_epoch=epoch;}
          }
          if (le16(p.data.data() + 1) == 0x0c26 && p.size >= 6)
            sixteen = (le16(p.data.data() + 4) & 0x20) != 0;
          last_opcode = le16(p.data.data() + 1);
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
        if (serialized_mode) {
          std::memcpy(t->buffer.data(), p.data.data(), p.size);
          v->endpoint=acl.out; v->type=LIBUSB_TRANSFER_TYPE_BULK; v->length=int(p.size);
        }
        tx_packets++;
        submit(t);
      }
    }
    pump_iso();
    if (serialized_mode) { pump_serialized_voice(); return; }
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
    readers[2].reset();
    readers[4].reset();
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
      synchronous_limit=capabilities.hci.value("sco_packet_bytes",0u);
      select_iso_transport();
      {
        std::lock_guard<std::mutex> lock(init_mutex);
        capabilities.voice_transport = (sco_interface >= 0 || (serialized_mode && synchronous_limit>=2)) &&
                                        capabilities.hci.value("voice_capable", false);
      }
      startup_complete = true;
      alive = true;
      initializing = false;
      SetEvent(ready);
      while (!stopping) {
        events();
        if (stopping)
          break;
        if(iso_requested && command_deadline.expired(ticks(),frequency()*5)) {
          command_failed=true;stopping=true;break;
        }
        if (!serialized_mode && wanted_alt != current_alt) change_alt();
        {
          uint16_t iso_handle;uint32_t iso_epoch;
          bool pacing = current_alt!=0 || (serialized_mode && target.voice_byte_rate()!=0) ||
                        (le_capabilities.transmit() && target.iso_transmit_route(iso_handle,iso_epoch));
          if (pace_voice.exchange(pacing)!=pacing) SetEvent(pace_wake);
        }
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
    {
      std::lock_guard<std::mutex> lock(init_mutex);
      if (capabilities.hci.contains("ready")) capabilities.hci["ready"] = false;
    }
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
      if (serialized_mode && acl_claimed) api.libusb_set_interface_alt_setting(device, acl.interface, 0);
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
        resolve_instance(std::move(resolver)) {
    link_lifetime.on_acquire(&target,[](void *context,uint16_t h){static_cast<HciTarget*>(context)->connected(h);});
  }
  ~Usb() { close(); }
  void request_iso(bool enabled) { require(!worker.joinable(), "Select ISO transport before starting USB"); iso_requested=enabled; }
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
    if (!alive || !p || n < 1 || n > 4096 || (p[0] != 1 && p[0] != 2))
      return false; // Real-time SCO/ISO is exclusively native.
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
  void poke() {
    std::lock_guard<std::mutex> lock(event_mutex);
    if(context) api.libusb_interrupt_event_handler(context);
  }
  bool pop(SmallPacket &p) { return controls.pop(p); }
  bool active() const { return alive.load(); }
  json startup() const {
    std::lock_guard<std::mutex> lock(init_mutex);
    std::array<uint8_t,9> terminal{};
    auto terminal_size=controller_evidence.copy(terminal.data(),uint32_t(terminal.size()));
    return {{"starting", initializing.load()},
            {"startup_complete", startup_complete.load()},
            {"firmware_state", firmware_state},
            {"firmware_progress", firmware_progress},
            {"capabilities", capabilities.snapshot()},
            {"le_audio", {{"transport", le_capabilities.mode()==IsoTransport::BulkSerialization ? "bulk_serialization" :
                                       le_capabilities.mode()==IsoTransport::IntelLegacyBulk ? "intel_legacy_bulk" : "unavailable"},
                          {"features_probed", le_capabilities.probed()},
                          {"unicast_receive", le_capabilities.receive()}, {"unicast_transmit", le_capabilities.transmit()},
                          {"broadcast_receive", le_capabilities.broadcast()},
                          {"iso_packet_length", le_capabilities.packet_length()}, {"iso_packets", le_capabilities.packet_count()}}},
            {"firmware_error", init_code},
            {"firmware_profile", init_profile},
            {"controller_terminal_event", hex(terminal.data(),terminal_size)},
            {"error", init_error}};
  }
  const char *terminal_reason() const {
    if(command_failed.load()) return "controller_command_failed";
    if (iso_flow_failed.load()) return "iso_flow_control";
    if (controller_failed.load()) return "controller_hardware_error";
    if (controller_restarted.load()) return "controller_restarted";
    if (callback_failed.load()) return "usb_callback_failed";
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
    result.update({{"last_transfer_status", last_transfer_status.load()},
                   {"last_submit_error", last_submit_error.load()},
                   {"callback_failed", callback_failed.load()},
                   {"hardware_error", hardware_error.load()},
                   {"last_hci_event", last_event.load()}, {"last_hci_opcode", last_opcode.load()},
                   {"last_receive_age_ms", last_receive.load() ? age_ticks(last_receive.load()) * 1000.0 / frequency() : -1.0}});
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
