/* Offline fault-injection backend for the production Usb class. Apache-2.0.
 * Never enumerates physical devices; exports only the API used by that class. */
#include <windows.h>
#include <libusb.h>
#include <stdlib.h>
#include <string.h>

static struct libusb_transfer *pending[8];
static ULONGLONG submitted_at[8];
static int cancelled[8], bad_release, claimed;
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
__declspec(dllexport) int fake_bad_release(void) { return bad_release; }
__declspec(dllexport) int fake_pending(void) {
  int n = 0;
  for (int i = 0; i < 8; i++)
    n += pending[i] != NULL;
  return n;
}
int LIBUSB_CALL libusb_init(libusb_context **context) {
  *context = (libusb_context *)1;
  return 0;
}
void LIBUSB_CALL libusb_exit(libusb_context *context) { (void)context; }
const struct libusb_version *LIBUSB_CALL libusb_get_version(void) { return &version; }
const char *LIBUSB_CALL libusb_error_name(int code) {
  return code == LIBUSB_ERROR_ACCESS ? "LIBUSB_ERROR_ACCESS" : "LIBUSB_ERROR_IO";
}
ssize_t LIBUSB_CALL libusb_get_device_list(libusb_context *context, libusb_device ***list) {
  (void)context;
  *list = calloc(2, sizeof(**list));
  (*list)[0] = (libusb_device *)1;
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
  descriptor->bDeviceClass = 0xe0;
  return 0;
}
int LIBUSB_CALL libusb_open(libusb_device *device, libusb_device_handle **handle) {
  (void)device;
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
  Sleep(2);
  for (int i = 0; i < 8; i++) {
    struct libusb_transfer *transfer = pending[i];
    if (!transfer)
      continue;
    int expired = transfer->timeout && GetTickCount64() - submitted_at[i] >= transfer->timeout;
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
