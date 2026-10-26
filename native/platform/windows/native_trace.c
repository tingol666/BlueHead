#define WIN32_LEAN_AND_MEAN
#include <windows.h>

static HANDLE output = INVALID_HANDLE_VALUE;
static volatile LONG busy = 0;

static void write_text(const char *text) {
  DWORD written;
  WriteFile(output, text, lstrlenA(text), &written, NULL);
}

static void address_line(void *address) {
  MEMORY_BASIC_INFORMATION info;
  char line[640], module[MAX_PATH];
  if (VirtualQuery(address, &info, sizeof(info)) &&
      GetModuleFileNameA((HMODULE)info.AllocationBase, module, MAX_PATH)) {
    wsprintfA(line, "%s+0x%lx\r\n", module,
              (unsigned long)((ULONG_PTR)address - (ULONG_PTR)info.AllocationBase));
  } else {
    wsprintfA(line, "unmapped code %p\r\n", address);
  }
  write_text(line);
}

static LONG CALLBACK trace(EXCEPTION_POINTERS *exception) {
  DWORD code = exception->ExceptionRecord->ExceptionCode;
  if (code != EXCEPTION_ACCESS_VIOLATION && code != EXCEPTION_ILLEGAL_INSTRUCTION &&
      code != EXCEPTION_STACK_OVERFLOW)
    return EXCEPTION_CONTINUE_SEARCH;
  if (InterlockedCompareExchange(&busy, 1, 0))
    return EXCEPTION_CONTINUE_SEARCH;
  char header[128];
  wsprintfA(header, "first-chance code=0x%lx thread=%lu\r\n", code, GetCurrentThreadId());
  write_text(header);
  address_line(exception->ExceptionRecord->ExceptionAddress);
  void *frames[32];
  USHORT count = CaptureStackBackTrace(0, 32, frames, NULL);
  for (USHORT i = 0; i < count; ++i)
    address_line(frames[i]);
  FlushFileBuffers(output);
  InterlockedExchange(&busy, 0);
  return EXCEPTION_CONTINUE_SEARCH;
}

__declspec(dllexport) int install_trace(const wchar_t *path) {
  output = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, NULL);
  if (output == INVALID_HANDLE_VALUE)
    return (int)GetLastError();
  return AddVectoredExceptionHandler(1, trace) ? 0 : (int)GetLastError();
}
