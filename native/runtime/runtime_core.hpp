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
      candidate->request_iso(request.value("le_audio", false));
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
      json packets = json::array(), packet_epochs=json::array();
      SmallPacket p;
      if (usb)
        while (packets.size() < 64 && usb->pop(p)) {
          packets.push_back(hex(p.data.data(), p.size));packet_epochs.push_back(p.link_epoch);
        }
      bool active = usb && usb->active();
      json result = {{"packets", packets},{"packet_epochs",packet_epochs},
                     {"active", active},
                     {"reason", active ? ""
                                : usb  ? usb->terminal_reason()
                                       : "stopped"}};
      if (usb)
        result.update(usb->startup());
      if (usb && !active && !result.value("starting", false))
        result["transport"] = usb->snapshot();
      return result;
    }
    bool prepare=op=="le_rx_prepare" || op=="le_tx_prepare";
    bool bind=op=="le_rx_bind" || op=="le_tx_bind";
    auto verify=[&] {
      auto parent=request.value("parent",0xffffu);
      auto parent_epoch=request.value("parent_epoch",uint64_t(0));
      require(parent<=0xeff || parent==0xffff,"Invalid LE parent handle");
      if(prepare && usb && !request.value("broadcast",false) && parent>0xeff) throw SourceExpired();
      if(parent<=0xeff && (usb || parent_epoch || audio.link_identity(uint16_t(parent))) && (!parent_epoch || audio.link_identity(uint16_t(parent))!=parent_epoch))
        throw SourceExpired();
      if(bind) {
        auto handle=request.at("handle").get<uint16_t>();auto identity=request.value("link_epoch",uint64_t(0));
        if((usb || identity || audio.link_identity(handle)) && (!identity || audio.link_identity(handle)!=identity)) throw SourceExpired();
      }
    };
    if(prepare || bind) verify();
    auto result = audio.command(request);
    if(prepare || bind) {
      try { verify(); }
      catch(...) {
        // Physical events are processed concurrently by the sole USB owner.
        // A link can retire during codec preparation. Keep its native gate shut.
        auto rollback=request;rollback["op"]=op.rfind("le_tx_",0)==0?"le_tx_stop":"le_rx_stop";
        try { audio.command(rollback); } catch(...) {}
        throw;
      }
    }
    if (usb && (op=="le_tx_bind" || op=="le_tx_stop")) usb->poke();
    if (op == "snapshot")
      result["usb"] = usb ? usb->snapshot() : json{{"active", false}};
    return result;
  }
};
} // namespace bh
