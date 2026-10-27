/* Application composition root. Only this type owns a physical USB transport.
 * AudioGraph sees bounded HCI ports, never libusb or an adapter handle. */
#pragma once
#include "audio_graph.hpp"
#include "runtime_usb.hpp"
namespace bh {
class Core final {
  bool offline;
  AudioGraph audio;
  std::unique_ptr<Usb> usb; // Declared last: closes and joins before audio is destroyed.
public:
  explicit Core(const fs::path &assets, bool offline_test = false)
      : offline(offline_test), audio(assets, offline_test) {}
  void quiesce_now() { audio.quiesce_now(); }
  json command(const json &request, const fs::path &assets) {
    auto op = request.at("op").get<std::string>();
    if (op == "usb_inventory") {
      require(!offline, "Hardware enumeration is disabled in offline-test mode");
      return controller_inventory(assets);
    }
    if (op == "usb_open") {
      require(!offline, "Hardware opening is disabled in offline-test mode");
      require(!usb, "USB owner already running");
      auto candidate = std::make_unique<Usb>(assets, audio);
      // Firmware upload can take seconds. Keep RPC/Stop responsive while the
      // same USB owner performs bounded bootstrap on its own event thread.
      if (request.contains("adapter_id")) candidate->start_id(request.at("adapter_id"));
      else candidate->start(request.at("vid"), request.at("pid"), request.value("index", 0u));
      usb = std::move(candidate);
      audio.transport_opened();
      return true;
    }
    if (op == "usb_close") {
      if (usb) {
        usb->close();
        usb.reset();
      }
      return true;
    }
    if (op == "usb_send") {
      auto p = unhex(request.at("packet"), 4096);
      require(usb && usb->send(p.data(), p.size()), "Native HCI transmit queue unavailable");
      return true;
    }
    if (op == "usb_poll") {
      json packets = json::array();
      SmallPacket p;
      if (usb)
        while (packets.size() < 64 && usb->pop(p))
          packets.push_back(hex(p.data.data(), p.size));
      bool active = usb && usb->active();
      json result = {{"packets", packets},
                     {"active", active},
                     {"reason", active ? ""
                                : usb  ? usb->terminal_reason()
                                       : "stopped"}};
      if (usb)
        result.update(usb->startup());
      return result;
    }
    auto result = audio.command(request);
    if (op == "snapshot")
      result["usb"] = usb ? usb->snapshot() : json{{"active", false}};
    return result;
  }
};
} // namespace bh
