/* BlueHead process-excluding WASAPI capture. Apache-2.0.
 * No files, network or Python callbacks. Default: process-tree exclusion
 * (Windows build 20348+). Optional isolated endpoint: guarded by EndpointActor.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <avrt.h>
#include <new>
#include <cstring>
#include <cmath>
#include <cwchar>

#define API extern "C" __declspec(dllexport)
static constexpr UINT CAPACITY = 4800;
// Documented audioclientactivationparams.h ABI, absent in some MinGW SDKs.
struct ProcessActivation {
  UINT type;
  DWORD pid;
  UINT mode;
};
static_assert(sizeof(ProcessActivation) == 12, "activation ABI");

class Activation final : public IActivateAudioInterfaceCompletionHandler {
  LONG references = 1;

public:
  HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  HRESULT result = E_PENDING;
  IUnknown *audio = nullptr;
  ~Activation() {
    if (audio)
      audio->Release();
    if (done)
      CloseHandle(done);
  }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id, void **out) override {
    if (!out)
      return E_POINTER;
    *out = nullptr;
    if (IsEqualIID(id, __uuidof(IUnknown)) ||
        IsEqualIID(id, __uuidof(IActivateAudioInterfaceCompletionHandler)) ||
        IsEqualIID(id, __uuidof(IAgileObject))) {
      *out = static_cast<IActivateAudioInterfaceCompletionHandler *>(this);
      AddRef();
      return S_OK;
    }
    return E_NOINTERFACE;
  }
  ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&references); }
  ULONG STDMETHODCALLTYPE Release() override {
    ULONG count = InterlockedDecrement(&references);
    if (!count)
      delete this;
    return count;
  }
  HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation *op) override {
    HRESULT activated = E_FAIL;
    result = op->GetActivateResult(&activated, &audio);
    if (SUCCEEDED(result))
      result = activated;
    SetEvent(done);
    return S_OK;
  }
};

struct Capture {
  SRWLOCK lock = SRWLOCK_INIT;
  HANDLE stop = nullptr, ready = nullptr, thread = nullptr;
  volatile LONG error = E_PENDING;
  PVOID volatile notify = nullptr;
  DWORD pid;
  wchar_t endpoint[512] = {};
  UINT read = 0, frames = 0;
  unsigned long long received = 0, dropped = 0, clock_position = 0, clock_arrival = 0;
  float pcm[CAPACITY] = {};
};

static DWORD WINAPI capture_thread(void *argument) {
  Capture *c = static_cast<Capture *>(argument);
  HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
  bool apartment = SUCCEEDED(hr);
  IAudioClient *client = nullptr;
  IAudioCaptureClient *reader = nullptr;
  IActivateAudioInterfaceAsyncOperation *operation = nullptr;
  Activation *activation = nullptr;
  HANDLE sample = nullptr, priority = nullptr;
  DWORD task = 0;
  bool started = false;
  IMMDeviceEnumerator *enumerator = nullptr;
  IMMDevice *endpoint = nullptr;
  ProcessActivation parameters = {1, c->pid, 1};
  PROPVARIANT variant = {};
  if (FAILED(hr))
    goto finished;
  if (c->endpoint[0]) {
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                          __uuidof(IMMDeviceEnumerator), reinterpret_cast<void **>(&enumerator));
    if (SUCCEEDED(hr))
      hr = enumerator->GetDevice(c->endpoint, &endpoint);
    if (SUCCEEDED(hr))
      hr = endpoint->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                              reinterpret_cast<void **>(&client));
    if (FAILED(hr))
      goto finished;
    goto initialize;
  }
  activation = new (std::nothrow) Activation();
  if (!activation || !activation->done) {
    hr = E_OUTOFMEMORY;
    goto finished;
  }
  {
    variant.vt = VT_BLOB;
    variant.blob.cbSize = sizeof(parameters);
    variant.blob.pBlobData = reinterpret_cast<BYTE *>(&parameters);
    hr = ActivateAudioInterfaceAsync(L"VAD\\Process_Loopback", __uuidof(IAudioClient), &variant,
                                     activation, &operation);
  }
  if (FAILED(hr))
    goto finished;
  {
    HANDLE waits[] = {c->stop, activation->done};
    DWORD wait = WaitForMultipleObjects(2, waits, FALSE, 4000);
    if (wait != WAIT_OBJECT_0 + 1) {
      hr = HRESULT_FROM_WIN32(wait == WAIT_TIMEOUT ? ERROR_TIMEOUT : ERROR_CANCELLED);
      goto finished;
    }
  }
  hr = activation->result;
  if (FAILED(hr))
    goto finished;
  if (!activation->audio) {
    hr = E_NOINTERFACE;
    goto finished;
  }
  hr =
      activation->audio->QueryInterface(__uuidof(IAudioClient), reinterpret_cast<void **>(&client));
  if (FAILED(hr))
    goto finished;
initialize:
  {
    WAVEFORMATEX format = {};
    format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
    format.nChannels = 2;
    format.nSamplesPerSec = 48000;
    format.wBitsPerSample = 32;
    format.nBlockAlign = 8;
    format.nAvgBytesPerSec = 384000;
    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                                AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                            0, 0, &format, nullptr);
  }
  if (FAILED(hr))
    goto finished;
  hr = client->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void **>(&reader));
  if (FAILED(hr))
    goto finished;
  sample = CreateEventW(nullptr, FALSE, FALSE, nullptr);
  if (!sample) {
    hr = HRESULT_FROM_WIN32(GetLastError());
    goto finished;
  }
  hr = client->SetEventHandle(sample);
  if (FAILED(hr))
    goto finished;
  hr = client->Start();
  if (FAILED(hr))
    goto finished;
  started = true;
  InterlockedExchange(&c->error, S_OK);
  SetEvent(c->ready);
  priority = AvSetMmThreadCharacteristicsW(L"Audio", &task);
  while (WaitForSingleObject(c->stop, 0) == WAIT_TIMEOUT) {
    HANDLE waits[] = {c->stop, sample};
    DWORD wait = WaitForMultipleObjects(2, waits, FALSE, 1000);
    if (wait == WAIT_OBJECT_0)
      break;
    if (wait == WAIT_TIMEOUT)
      continue; // Silent applications need not produce packets.
    if (wait != WAIT_OBJECT_0 + 1) {
      hr = HRESULT_FROM_WIN32(GetLastError());
      break;
    }
    UINT available = 0;
    while (SUCCEEDED(hr = reader->GetNextPacketSize(&available)) && available) {
      BYTE *bytes = nullptr;
      UINT frames = 0;
      DWORD flags = 0;
      UINT64 position = 0, qpc = 0;
      hr = reader->GetBuffer(&bytes, &frames, &flags, &position, &qpc);
      if (FAILED(hr))
        break;
      AcquireSRWLockExclusive(&c->lock);
      if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) {
        c->dropped += c->frames;
        c->read = c->frames = 0;
      }
      const float *data = reinterpret_cast<const float *>(bytes);
      for (UINT i = 0; i < frames; i++) {
        float value = (flags & AUDCLNT_BUFFERFLAGS_SILENT) || !data
                          ? 0
                          : (data[i * 2] + data[i * 2 + 1]) * .5f;
        if (!std::isfinite(value))
          value = 0;
        if (c->frames == CAPACITY) {
          c->read = (c->read + 1) % CAPACITY;
          c->frames--;
          c->dropped++;
        }
        c->pcm[(c->read + c->frames) % CAPACITY] = value;
        c->frames++;
      }
      c->received += frames;
      c->clock_position = position + frames;
      LARGE_INTEGER now;
      QueryPerformanceCounter(&now);
      // Producer receipt timing, never bridge-drain time. The optional
      // WASAPI qpc is in 100ns units, not raw QueryPerformanceCounter ticks.
      c->clock_arrival = now.QuadPart;
      ReleaseSRWLockExclusive(&c->lock);
      HANDLE notify = InterlockedCompareExchangePointer(&c->notify, nullptr, nullptr);
      if (notify)
        SetEvent(notify);
      hr = reader->ReleaseBuffer(frames);
      if (FAILED(hr))
        break;
    }
    if (FAILED(hr))
      break;
  }
finished:
  if (priority)
    AvRevertMmThreadCharacteristics(priority);
  if (started)
    client->Stop();
  if (reader)
    reader->Release();
  if (client)
    client->Release();
  if (endpoint)
    endpoint->Release();
  if (enumerator)
    enumerator->Release();
  if (operation)
    operation->Release();
  if (activation)
    activation->Release();
  if (sample)
    CloseHandle(sample);
  InterlockedExchange(&c->error, hr);
  SetEvent(c->ready);
  if (apartment)
    CoUninitialize();
  return 0;
}

static Capture *open_capture(DWORD excluded_pid, const wchar_t *endpoint, DWORD *error) {
  Capture *c = new (std::nothrow) Capture();
  if (!c) {
    if (error)
      *error = E_OUTOFMEMORY;
    return nullptr;
  }
  c->pid = excluded_pid;
  if (endpoint) {
    if (!*endpoint || std::wcslen(endpoint) >= 512) {
      if (error) *error = E_INVALIDARG;
      delete c;
      return nullptr;
    }
    std::wcscpy(c->endpoint, endpoint);
  }
  c->stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  c->ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (c->stop && c->ready)
    c->thread = CreateThread(nullptr, 0, capture_thread, c, 0, nullptr);
  if (!c->thread) {
    if (error)
      *error = HRESULT_FROM_WIN32(GetLastError());
    if (c->stop)
      CloseHandle(c->stop);
    if (c->ready)
      CloseHandle(c->ready);
    delete c;
    return nullptr;
  }
  // The activation itself has a bounded wait; never free a running thread's state.
  WaitForSingleObject(c->ready, INFINITE);
  if (FAILED(c->error)) {
    if (error)
      *error = c->error;
    SetEvent(c->stop);
    WaitForSingleObject(c->thread, INFINITE);
    CloseHandle(c->thread);
    CloseHandle(c->ready);
    CloseHandle(c->stop);
    delete c;
    return nullptr;
  }
  if (error)
    *error = S_OK;
  return c;
}
API Capture *bh_loop_open(DWORD excluded_pid, DWORD *error) {
  return open_capture(excluded_pid, nullptr, error);
}
// Endpoint loopback has no OS-level process exclusion. The native endpoint
// actor must isolate it from every BlueHead render route before publishing PCM.
API Capture *bh_loop_open_endpoint(const wchar_t *endpoint, DWORD *error) {
  return endpoint ? open_capture(0, endpoint, error) : nullptr;
}
API int bh_loop_read(Capture *c, float *out, UINT count) {
  if (!c || !out || count > CAPACITY)
    return -1;
  if (FAILED(c->error))
    return -1;
  AcquireSRWLockExclusive(&c->lock);
  UINT take = count < c->frames ? count : c->frames;
  for (UINT i = 0; i < take; i++)
    out[i] = c->pcm[(c->read + i) % CAPACITY];
  c->read = (c->read + take) % CAPACITY;
  c->frames -= take;
  ReleaseSRWLockExclusive(&c->lock);
  return take;
}
API DWORD bh_loop_error(Capture *c) { return c ? c->error : E_POINTER; }
API int bh_loop_clock(Capture *c, unsigned long long *position, unsigned long long *arrival) {
  if (!c || !position || !arrival)
    return -1;
  AcquireSRWLockShared(&c->lock);
  *position = c->clock_position;
  *arrival = c->clock_arrival;
  ReleaseSRWLockShared(&c->lock);
  return *arrival ? 1 : 0;
}
API int bh_loop_close(Capture *c) {
  if (!c)
    return 1;
  SetEvent(c->stop);
  if (WaitForSingleObject(c->thread, 2000) != WAIT_OBJECT_0)
    return 0;
  CloseHandle(c->thread);
  CloseHandle(c->ready);
  CloseHandle(c->stop);
  delete c;
  return 1;
}

// Borrowed event remains alive until capture thread is joined.
API void bh_loop_ready_event(Capture *c, void *event) {
  if (c)
    InterlockedExchangePointer(&c->notify, event);
}
