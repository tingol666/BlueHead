/* Offline fault-injection backend for the production Usb class. Apache-2.0.
 * Never enumerates physical devices; exports only the API used by that class. */
#include <windows.h>
#include <libusb.h>
#include <stdlib.h>
#include <string.h>

static struct libusb_transfer *pending[8];
static ULONGLONG submitted_at[8];
static int cancelled[8], bad_release, claimed, opened_device;
static struct libusb_version version = {1, 0, 30, 0, "", ""};
static struct libusb_endpoint_descriptor endpoints[3] = {
    {.bEndpointAddress = 0x81, .bmAttributes = 3, .wMaxPacketSize = 16},
    {.bEndpointAddress = 0x82, .bmAttributes = 2, .wMaxPacketSize = 64},
    {.bEndpointAddress = 0x02, .bmAttributes = 2, .wMaxPacketSize = 64}};
static struct libusb_interface_descriptor alt = {
    .bNumEndpoints = 3, .bInterfaceClass = 0xe0, .endpoint = endpoints};
static struct libusb_interface interface = {.altsetting = &alt, .num_altsetting = 1};
static struct libusb_config_descriptor config = {.bNumInterfaces = 1, .interface = &interface};
static int mode(const char *value) {
  const char *setting = getenv("BLUEHEAD_USB_FAULT");
  return setting && !strcmp(setting, value);
}
static int intel(void) {
  const char *setting = getenv("BLUEHEAD_USB_FAULT");
  return setting && !strncmp(setting, "intel-", 6);
}
static int intel_boot = -1, upload_commands, bulk_violations, firmware_loads;
static int startup_finished, realtek_loaded, realtek_fragments;
static int realtek(void) { const char *value = getenv("BLUEHEAD_USB_FAULT"); return value && !strncmp(value, "realtek-", 8); }
static unsigned payload_bytes;
static struct { unsigned char data[260]; int length, endpoint, offset; } replies[16];
static unsigned reply_read, reply_write;
static void reply_event(int endpoint, const unsigned char *data, int length) {
  if (reply_write - reply_read >= 16 || length > 260) { bulk_violations++; return; }
  unsigned slot = reply_write++ % 16;
  memcpy(replies[slot].data, data, (size_t)length);
  replies[slot].length = length; replies[slot].endpoint = endpoint; replies[slot].offset = 0;
}
static void complete_command(unsigned opcode, unsigned status, const unsigned char *extra, int size, int bulk) {
  unsigned char p[260] = {0x0e, (unsigned char)(4 + size), 1, (unsigned char)opcode, (unsigned char)(opcode >> 8), (unsigned char)status};
  if (size) memcpy(p + 6, extra, (size_t)size);
  reply_event(bulk ? 0x82 : 0x81, p, size + 6);
}
static int standard_write(struct libusb_transfer *t) {
  const unsigned char *p = t->buffer + (t->type == LIBUSB_TRANSFER_TYPE_BULK ? 0 : LIBUSB_CONTROL_SETUP_SIZE);
  unsigned op = p[0] | ((unsigned)p[1] << 8);
  unsigned char version_reply[] = {12, 1, 0, 12, 10, 0, 0x34, 0x12};
  unsigned char features[] = {0, 8, 0, 0x80, 0, 0, 0, 0};
  unsigned char buffers[] = {0xff, 0x03, 64, 8, 0, 4, 0};
  unsigned char address[] = {1, 2, 3, 4, 5, 6};
  if (op == 0x0c03) complete_command(op, 0, NULL, 0, 0);
  else if (op == 0x1001) {
    if (mode("standard-timeout")) return 1;
    if (realtek()) { version_reply[0] = 10; version_reply[1] = realtek_loaded ? 0xc6 : 11; version_reply[2] = realtek_loaded ? 0xdf : 0; version_reply[4] = 93;
      version_reply[6] = realtek_loaded ? 0x22 : 0x61; version_reply[7] = realtek_loaded ? 0xd9 : 0x87; }
    complete_command(op, 0, version_reply, sizeof(version_reply), 0);
  } else if (op == 0x1003) {
    if (mode("standard-le-only")) features[4] = 0x20;
    complete_command(op, 0, features, mode("standard-malformed") ? 3 : (int)sizeof(features), 0);
  } else if (op == 0x1005) {
    if (mode("standard-no-buffer")) buffers[3] = 0;
    complete_command(op, 0, buffers, sizeof(buffers), 0);
  } else if (op == 0x1009) {
    if (mode("standard-no-address")) memset(address, 0, sizeof(address));
    complete_command(op, 0, address, sizeof(address), 0); startup_finished = 1;
  } else if (realtek() && op == 0xfc6d) { unsigned char rom = 0; complete_command(op, 0, &rom, 1, 0); }
  else if (realtek() && op == 0xfc20) {
    unsigned char index = p[3];
    if (mode("realtek-download-timeout")) return 1;
    if ((index & 0x7f) != (realtek_fragments ? 1 + (realtek_fragments - 1) % 127 : 0)) bulk_violations++;
    realtek_fragments++; upload_commands++;
    if (index & 0x80) { realtek_loaded = 1; firmware_loads++; }
    if (mode("realtek-bad-ack")) index ^= 1;
    complete_command(op, 0, &index, 1, 0);
  } else return 0;
  return 1;
}
static void intel_write(struct libusb_transfer *t) {
  int bulk = t->type == LIBUSB_TRANSFER_TYPE_BULK;
  const unsigned char *p = t->buffer + (bulk ? 0 : LIBUSB_CONTROL_SETUP_SIZE);
  unsigned opcode = p[0] | ((unsigned)p[1] << 8);
  int size = p[2]; p += 3;
  if (opcode == 0x0c03) complete_command(opcode, intel_boot == 1 ? 1 : 0, NULL, 0, 0);
  else if (opcode == 0xfc05) {
    // Actual AX211 CNVI/CNVR/hardware fields from a read-only version query.
    unsigned char fields[] = {0x10,4,1,4,8,0, 0x11,4,0x10,4,0x40,0, 0x12,4,0,0x37,0x19,0,
                              0x1c,1,(unsigned char)intel_boot, 0x2f,1,1};
    if (mode("intel-unsupported")) fields[2] = 2;
    complete_command(opcode, 0, fields, sizeof(fields), 0);
  } else if (opcode == 0xfc09) {
    upload_commands++;
    if (!bulk || size < 2 || size > 253) bulk_violations++;
    if (mode("intel-command-timeout")) return;
    if (p[0] == 1) payload_bytes += (unsigned)(size - 1);
    complete_command(opcode, mode("intel-command-reject") ? 0x0c : 0, NULL, 0, 1);
    if (payload_bytes == 720088) {
      // Actual successful AX211 response uses opcode=0, not necessarily fc09.
      unsigned char done[] = {0xff,5,6,0,0,0,0};
      if (mode("intel-signature-reject")) done[3] = 1;
      reply_event(0x82, done, sizeof(done));
    }
  } else if (opcode == 0xfc01) {
    unsigned char boot[] = {0xff,7,2,0,1,0,0,0,0};
    if (mode("intel-boot-metadata")) { boot[5] = 2; boot[7] = 1; boot[8] = 1; }
    if (size != 8 || p[0] || p[1] != 1 || p[2] || p[3] != 1 ||
        p[4] || p[5] != 8 || p[6] != 0x10 || p[7]) bulk_violations++;
    intel_boot = 3; firmware_loads++;
    if (!mode("intel-boot-timeout")) reply_event(0x82, boot, sizeof(boot));
  } else if (opcode == 0xfc8b) {
    if (intel_boot != 3 || bulk || size < 3) bulk_violations++;
    complete_command(opcode, mode("intel-ddc-reject") ? 0x0c : 0, NULL, 0, 0);
  } else if (!standard_write(t)) bulk_violations++;
}
__declspec(dllexport) int fake_upload_commands(void) { return upload_commands; }
__declspec(dllexport) int fake_bulk_violations(void) { return bulk_violations; }
__declspec(dllexport) int fake_firmware_loads(void) { return firmware_loads; }
__declspec(dllexport) int fake_bad_release(void) { return bad_release; }
__declspec(dllexport) int fake_opened_device(void) { return opened_device; }
__declspec(dllexport) int fake_pending(void) {
  int n = 0;
  for (int i = 0; i < 8; i++)
    n += pending[i] != NULL;
  return n;
}
int LIBUSB_CALL libusb_init(libusb_context **context) {
  if (intel_boot == -1) intel_boot = mode("intel-warm") ? 3 : 1;
  *context = (libusb_context *)1;
  startup_finished = 0; reply_read = reply_write = 0;
  return 0;
}
void LIBUSB_CALL libusb_exit(libusb_context *context) { (void)context; }
const struct libusb_version *LIBUSB_CALL libusb_get_version(void) { return &version; }
unsigned long LIBUSB_CALL libusb_get_session_data(libusb_device *device) {
  const char *value = getenv("BLUEHEAD_USB_FAULT");
  return value && !strncmp(value, "identity-", 9) ? (unsigned long)(uintptr_t)device : 0;
}
const char *LIBUSB_CALL libusb_error_name(int code) {
  return code == LIBUSB_ERROR_ACCESS ? "LIBUSB_ERROR_ACCESS" : "LIBUSB_ERROR_IO";
}
ssize_t LIBUSB_CALL libusb_get_device_list(libusb_context *context, libusb_device ***list) {
  (void)context;
  *list = calloc(3, sizeof(**list));
  (*list)[0] = (libusb_device *)1;
  if (mode("identity-reorder")) { (*list)[0] = (libusb_device *)2; (*list)[1] = (libusb_device *)1; return 2; }
  if (mode("identity-normal") || mode("identity-legacy")) { (*list)[1] = (libusb_device *)2; return 2; }
  if (mode("intel-ambiguous")) { (*list)[1] = (libusb_device *)2; return 2; }
  return 1;
}
void LIBUSB_CALL libusb_free_device_list(libusb_device **list, int unref) {
  (void)unref;
  free(list);
}
int LIBUSB_CALL libusb_get_device_descriptor(libusb_device *device,
                                             struct libusb_device_descriptor *descriptor) {
  (void)device;
  memset(descriptor, 0, sizeof(*descriptor));
  descriptor->idVendor = 0x1234;
  descriptor->idProduct = 0xabcd;
  if (intel()) { descriptor->idVendor = 0x8087; descriptor->idProduct = 0x0033; }
  if (realtek()) { descriptor->idVendor = 0x0bda; descriptor->idProduct = mode("realtek-unknown-board") ? 1 : 0x8771; }
  descriptor->bDeviceClass = 0xe0;
  return 0;
}
int LIBUSB_CALL libusb_open(libusb_device *device, libusb_device_handle **handle) {
  opened_device = (int)(uintptr_t)device;
  *handle = mode("open-denied") ? NULL : (libusb_device_handle *)1;
  return *handle ? 0 : LIBUSB_ERROR_ACCESS;
}
void LIBUSB_CALL libusb_close(libusb_device_handle *handle) {
  (void)handle;
  bad_release += fake_pending() != 0 || claimed != 0;
}
int LIBUSB_CALL libusb_get_active_config_descriptor(libusb_device *device,
                                                    struct libusb_config_descriptor **value) {
  (void)device;
  *value = &config;
  return 0;
}
int LIBUSB_CALL libusb_get_config_descriptor(libusb_device *device, uint8_t index,
                                             struct libusb_config_descriptor **value) {
  (void)index;
  return libusb_get_active_config_descriptor(device, value);
}
void LIBUSB_CALL libusb_free_config_descriptor(struct libusb_config_descriptor *value) {
  (void)value;
}
int LIBUSB_CALL libusb_claim_interface(libusb_device_handle *handle, int number) {
  (void)handle;
  (void)number;
  claimed++;
  return 0;
}
int LIBUSB_CALL libusb_release_interface(libusb_device_handle *handle, int number) {
  (void)handle;
  (void)number;
  if (claimed <= 0)
    bad_release++;
  else
    claimed--;
  return 0;
}
int LIBUSB_CALL libusb_set_interface_alt_setting(libusb_device_handle *handle, int number,
                                                 int setting) {
  (void)handle;
  (void)number;
  (void)setting;
  return 0;
}
struct libusb_transfer *LIBUSB_CALL libusb_alloc_transfer(int iso) {
  return calloc(1, sizeof(struct libusb_transfer) +
                       (size_t)iso * sizeof(struct libusb_iso_packet_descriptor));
}
void LIBUSB_CALL libusb_free_transfer(struct libusb_transfer *transfer) {
  for (int i = 0; i < 8; i++)
    bad_release += pending[i] == transfer;
  free(transfer);
}
int LIBUSB_CALL libusb_submit_transfer(struct libusb_transfer *transfer) {
  if (mode("submit-failed"))
    return LIBUSB_ERROR_IO;
  for (int i = 0; i < 8; i++)
    if (!pending[i]) {
      pending[i] = transfer;
      submitted_at[i] = GetTickCount64();
      cancelled[i] = 0;
      return 0;
    }
  return LIBUSB_ERROR_BUSY;
}
int LIBUSB_CALL libusb_cancel_transfer(struct libusb_transfer *transfer) {
  for (int i = 0; i < 8; i++)
    if (pending[i] == transfer) {
      cancelled[i] = 1;
      return 0;
    }
  return LIBUSB_ERROR_NOT_FOUND;
}
int LIBUSB_CALL libusb_handle_events_timeout_completed(libusb_context *context,
                                                       struct timeval *timeout, int *completed) {
  (void)context;
  (void)timeout;
  (void)completed;
  Sleep(intel() && !mode("intel-cancel") ? 0 : 2);
  for (int i = 0; i < 8; i++) {
    struct libusb_transfer *transfer = pending[i];
    if (!transfer)
      continue;
    int expired = transfer->timeout && GetTickCount64() - submitted_at[i] >= transfer->timeout;
    if (!cancelled[i] && !expired) {
      if (!(transfer->endpoint & 0x80)) {
        if (mode("control-timeout") && startup_finished) continue;
        if (intel()) intel_write(transfer); else standard_write(transfer);
        pending[i] = NULL; transfer->actual_length = transfer->length;
        transfer->status = LIBUSB_TRANSFER_COMPLETED; transfer->callback(transfer);
        continue;
      }
      if (reply_read != reply_write) {
        unsigned slot = reply_read % 16;
        if (replies[slot].endpoint == transfer->endpoint) {
          int n = replies[slot].length - replies[slot].offset;
          if (mode("intel-fragmented") && n > 3) n = 3;
          memcpy(transfer->buffer, replies[slot].data + replies[slot].offset, (size_t)n);
          replies[slot].offset += n;
          if (replies[slot].offset == replies[slot].length) reply_read++;
          pending[i] = NULL; transfer->actual_length = n;
          transfer->status = LIBUSB_TRANSFER_COMPLETED; transfer->callback(transfer);
          continue;
        }
      }
    }
    if (!cancelled[i] && !expired)
      continue;
    pending[i] = NULL;
    transfer->actual_length = 0;
    transfer->status = cancelled[i] ? LIBUSB_TRANSFER_CANCELLED : LIBUSB_TRANSFER_TIMED_OUT;
    transfer->callback(transfer);
  }
  return 0;
}
void LIBUSB_CALL libusb_interrupt_event_handler(libusb_context *context) { (void)context; }
