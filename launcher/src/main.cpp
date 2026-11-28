/**
 * @file main.cpp
 * @brief 适配操作系统进程入口；Windows 宽字符参数转换后再进入公共 launcher。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
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
/** @brief 把 Windows UTF-16 参数转为 UTF-8；转换失败返回空字符串。 */
std::string utf8(const std::wstring& text) {
    if (text.empty()) return std::string();
    int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), (int)text.size(), nullptr, 0, nullptr, nullptr);
    if (size <= 0) return std::string();
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), (int)text.size(), result.data(), size, nullptr, nullptr);
    return result;
}

/** @brief 转换宽字符参数并调用公共入口，返回应用退出码。 */
int run_wide(int argc, wchar_t** argv) {
    std::vector<std::string> args;
    args.reserve(argc > 0 ? (size_t)argc : 0);
    for (int i = 0; i < argc; ++i) args.emplace_back(utf8(argv[i]));
    return PyUpper::run(args);
}

}  // namespace

#ifdef APPBUILD_CONSOLE
/** @brief 调试控制台入口；转交宽字符参数及退出码。 */
int wmain(int argc, wchar_t** argv) { return run_wide(argc, argv); }
#else
/** @brief GUI 子系统入口；解析系统命令行，调用后用 LocalFree 释放参数数组。 */
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
/** @brief POSIX 入口；保留参数顺序并返回公共入口退出码。 */
int main(int argc, char** argv) {
    std::vector<std::string> args;
    args.reserve(argc > 0 ? (size_t)argc : 0);
    for (int i = 0; i < argc; ++i) args.emplace_back(argv[i]);
    return PyUpper::run(args);
}
#endif
