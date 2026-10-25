/* Small Unicode launcher. Apache-2.0. Runs the bundled desktop without a shell. */
#include <windows.h>
#include <wchar.h>
#include <stdlib.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous;
    wchar_t path[32768];
    if (!GetModuleFileNameW(NULL, path, 32768)) return 2;
    wchar_t *last = wcsrchr(path, L'\\');
    if (!last) return 2;
    *last = 0;
    const wchar_t *suffix = L"\\app\\BlueHead.exe";
    size_t length = wcslen(path) + wcslen(suffix) + wcslen(arguments) + 8;
    wchar_t *command = calloc(length, sizeof(wchar_t));
    if (!command) return 2;
    _snwprintf_s(command, length, _TRUNCATE, L"\"%s%s\" %s", path, suffix, arguments);
    STARTUPINFOW startup = {0}; PROCESS_INFORMATION process = {0};
    startup.cb = sizeof(startup); startup.dwFlags = STARTF_USESHOWWINDOW; startup.wShowWindow = show;
    if (!CreateProcessW(NULL, command, NULL, NULL, FALSE, 0, NULL, path, &startup, &process)) {
        MessageBoxW(NULL, L"BlueHead 文件不完整。请重新解压完整安装包，或重新安装。", L"BlueHead", MB_OK | MB_ICONINFORMATION);
        free(command); return 2;
    }
    free(command); CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD result = 2; GetExitCodeProcess(process.hProcess, &result); CloseHandle(process.hProcess);
    return (int)result;
}
