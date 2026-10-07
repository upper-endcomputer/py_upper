#include "PyUpper.h"
#include <string>
#include <vector>

#ifdef _WIN32
#include <windows.h>

namespace {

// The launcher is compiled as a GUI-subsystem executable so double-clicking a
// packaged application does not open a console window, and the wide command
// line is the only lossless source of a non-ASCII path on Windows. Converting
// it to UTF-8 here keeps every path inside the launcher in one encoding.
std::string utf8(const std::wstring& text) {
    if (text.empty()) return std::string();
    int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), (int)text.size(), nullptr, 0, nullptr, nullptr);
    if (size <= 0) return std::string();
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), (int)text.size(), result.data(), size, nullptr, nullptr);
    return result;
}

int run_wide(int argc, wchar_t** argv) {
    std::vector<std::string> args;
    args.reserve(argc > 0 ? (size_t)argc : 0);
    for (int i = 0; i < argc; ++i) args.emplace_back(utf8(argv[i]));
    return PyUpper::run(args);
}

}  // namespace

#ifdef PY_UPPER_CONSOLE
int wmain(int argc, wchar_t** argv) { return run_wide(argc, argv); }
#else
int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    int argc = 0;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == nullptr) return 1;
    int code = run_wide(argc, argv);
    LocalFree(argv);
    return code;
}
#endif

#else
int main(int argc, char** argv) {
    std::vector<std::string> args;
    args.reserve(argc > 0 ? (size_t)argc : 0);
    for (int i = 0; i < argc; ++i) args.emplace_back(argv[i]);
    return PyUpper::run(args);
}
#endif
