#include "PyUpper.h"
// Including Python.h must not drag the CPython import library into the link.
// The launcher resolves every entry point from the bundled libpython at
// runtime, so it has no use for python3XX.lib and no search path that holds
// one. Windows' pyconfig.h does not know that: unless the shared build is
// switched off it asks the linker for the library with
// #pragma comment(lib, "python313.lib"), and the launcher build then fails
// with LNK1104 against a file that was never meant to be there. On the other
// platforms pyconfig.h does not read this macro at all.
#define Py_NO_ENABLE_SHARED 1
#include <Python.h>
#include <algorithm>
#include <codecvt>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <locale>
#include <stdexcept>
#include <string>
#include <vector>
#ifdef _WIN32
#include <io.h>
#include <windows.h>
#else
#include <dlfcn.h>
#include <unistd.h>
#endif
#if defined(__APPLE__)
#include <CoreFoundation/CoreFoundation.h>
#endif

namespace fs = std::filesystem;

#ifdef PY_UPPER_HAS_EMBEDDED_SOURCE
// The entry programs, written by tools/py_upper at packaging time; see
// write_embedded_source(). Declared here, at file scope, because that is where
// the generated translation unit defines them: a declaration inside the
// anonymous namespace below would name a different, internal symbol and the
// link would fail with "has internal linkage but is not defined".
namespace py_upper_embedded {
extern const char* const kEntry;
extern const char* const kSmoke;
extern const char* const kEggLoader;
extern const char* const kEggKey;
extern const char* const kEggBackend;
}  // namespace py_upper_embedded
#endif

namespace {

// CPython is never linked into the launcher: every entry point is resolved from
// the bundled libpython at runtime, so one launcher binary works with any
// target runtime ABI the build tool ships next to it.
using ConfigInit = void (*)(PyConfig*);
using ConfigSetString = PyStatus (*)(PyConfig*, wchar_t**, const wchar_t*);
using WideAppend = PyStatus (*)(PyWideStringList*, const wchar_t*);
using InitializeFromConfig = PyStatus (*)(const PyConfig*);
using ConfigClear = void (*)(PyConfig*);
using FinalizeEx = int (*)();
using IsInitialized = int (*)();
using RunSimpleString = int (*)(const char*);

// The first entry point the launcher resolves, and therefore the symbol that
// decides which python DLL in the runtime directory is the runtime. python3.dll
// does not export it: the stable ABI it re-exports leaves the PyConfig API out.
constexpr const char* kRuntimeEntrySymbol = "PyConfig_InitIsolatedConfig";

// Entry file that does not depend on the executable name, so a launcher built
// without an embedded entry still finds its application after being renamed
// (PyStand's _pystand_static.int).
constexpr const char* kStaticEntry = "_py_upper_static.int";

// The encrypted application archive. It lives in its own directory so the
// bundle root stays readable, and one name keeps the build and the loader in
// sync without another setting.
constexpr const char* kEggDir = "bundle";
constexpr const char* kEggFile = "py_upper.dat";

// Fatal errors must be visible: on Windows the launcher is a GUI-subsystem
// binary and a macOS bundle started from Finder has no terminal, so a broken
// package would otherwise look like nothing happened at all. Non-interactive
// runs (smoke tests, CI) opt out instead of blocking on a dialog.
constexpr const char* kNoDialogEnv = "PY_UPPER_NO_DIALOG";
constexpr unsigned kDialogTimeoutSeconds = 120;
constexpr std::size_t kMaxDialogChars = 4000;

#ifdef _WIN32
std::wstring W(const std::string& s) {
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int)s.size(), nullptr, 0);
    if (n <= 0) throw std::runtime_error("UTF-8 conversion failed");
    std::wstring r(n, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int)s.size(), r.data(), n);
    return r;
}
void* symbol(void* h, const char* n) { return reinterpret_cast<void*>(GetProcAddress((HMODULE)h, n)); }
void unload(void* h) { if (h) FreeLibrary((HMODULE)h); }
unsigned long process_id() { return (unsigned long)GetCurrentProcessId(); }
#else
std::wstring W(const std::string& s) { return std::wstring_convert<std::codecvt_utf8<wchar_t>>().from_bytes(s); }
void* symbol(void* h, const char* n) { return dlsym(h, n); }
void unload(void* h) { if (h) dlclose(h); }
unsigned long process_id() { return (unsigned long)getpid(); }
#endif

// Every string that reaches the launcher is UTF-8: main.cpp converts the wide
// Windows command line, and Unix is UTF-8 already. std::filesystem converts a
// narrow string with the ANSI code page on Windows, so a plain
// fs::path(std::string) would mangle a non-ASCII install path. These two helpers
// are the only sanctioned conversions between the two encodings.
#ifdef _WIN32
fs::path path_from_utf8(const std::string& text) { return fs::u8path(text); }
std::string path_to_utf8(const fs::path& path) { return path.u8string(); }
#else
fs::path path_from_utf8(const std::string& text) { return fs::path(text); }
std::string path_to_utf8(const fs::path& path) { return path.string(); }
#endif

void set_env(const std::string& name, const std::string& value) {
#ifdef _WIN32
    SetEnvironmentVariableW(W(name).c_str(), W(value).c_str());
#else
    setenv(name.c_str(), value.c_str(), 1);
#endif
}

bool visible_stream() {
#ifdef _WIN32
    return _isatty(_fileno(stdout)) != 0 || _isatty(_fileno(stderr)) != 0;
#else
    return isatty(STDOUT_FILENO) != 0 || isatty(STDERR_FILENO) != 0;
#endif
}

// A GUI-subsystem launcher has no console of its own. When it is started from
// cmd.exe, PowerShell or a CI shell it joins the parent console so its own
// diagnostics still reach the caller. Streams that already exist win, because
// `> run.log` must keep capturing the launcher and the application.
void attach_parent_console() {
#ifdef _WIN32
    if (!AttachConsole(ATTACH_PARENT_PROCESS)) return;
    auto missing = [](DWORD id) {
        HANDLE handle = GetStdHandle(id);
        return handle == nullptr || handle == INVALID_HANDLE_VALUE;
    };
    FILE* stream = nullptr;
    if (missing(STD_OUTPUT_HANDLE)) freopen_s(&stream, "CONOUT$", "w", stdout);
    if (missing(STD_ERROR_HANDLE)) freopen_s(&stream, "CONOUT$", "w", stderr);
#endif
}

#if !defined(_WIN32) && !defined(__APPLE__)
std::string shell_quote(const std::string& text) {
    std::string quoted = "'";
    for (char c : text) {
        if (c == '\'') quoted += "'\\''";
        else quoted += c;
    }
    return quoted + "'";
}
#endif

void show_error(const std::string& title, const std::string& message) {
#ifdef _WIN32
    try {
        MessageBoxW(nullptr, W(message).c_str(), W(title).c_str(), MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
    } catch (...) {
        MessageBoxA(nullptr, message.c_str(), title.c_str(), MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
    }
#elif defined(__APPLE__)
    // CFUserNotification is the only native dialog reachable without linking
    // Cocoa. A session without a window server (CI, ssh) simply fails here and
    // leaves the stderr message as the only report.
    CFStringRef heading = CFStringCreateWithBytes(nullptr, (const UInt8*)title.data(), (CFIndex)title.size(), kCFStringEncodingUTF8, false);
    CFStringRef body = CFStringCreateWithBytes(nullptr, (const UInt8*)message.data(), (CFIndex)message.size(), kCFStringEncodingUTF8, false);
    if (heading == nullptr || body == nullptr) {
        if (heading) CFRelease(heading);
        if (body) CFRelease(body);
        return;
    }
    const void* keys[] = { kCFUserNotificationAlertHeaderKey, kCFUserNotificationAlertMessageKey };
    const void* values[] = { heading, body };
    CFDictionaryRef info = CFDictionaryCreate(nullptr, keys, values, 2, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    SInt32 error = 0;
    CFUserNotificationRef notification = CFUserNotificationCreate(nullptr, 0, kCFUserNotificationStopAlertLevel, &error, info);
    if (notification) {
        CFOptionFlags response = 0;
        // Bounded on purpose: an unattended failure must never wedge a build.
        CFUserNotificationReceiveResponse(notification, kDialogTimeoutSeconds, &response);
        CFRelease(notification);
    }
    if (info) CFRelease(info);
    CFRelease(heading);
    CFRelease(body);
#else
    std::string text = title + "\n\n" + message;
    if (std::system(("zenity --error --title=" + shell_quote(title) + " --text=" + shell_quote(text) + " 2>/dev/null").c_str()) != 0) {
        std::system(("xmessage -center " + shell_quote(text) + " 2>/dev/null").c_str());
    }
#endif
}

std::string clip_for_dialog(const std::string& text) {
    if (text.size() <= kMaxDialogChars) return text;
    return text.substr(0, kMaxDialogChars) + "\n... (truncated)";
}

void report_fatal(const std::string& message) {
    std::cerr << "py_upper: " << message << "\n";
    if (std::getenv(kNoDialogEnv)) return;
    if (visible_stream()) return;
    show_error("py_upper", clip_for_dialog(message));
}

fs::path exe_dir(const std::string& exe) {
    // argv[0] can be relative ("./Upper"), which would leave a "." component
    // in the parent chain and misplace the bundle root. Normalise first.
    std::error_code ec;
    auto absolute = fs::absolute(path_from_utf8(exe), ec);
    if (ec) absolute = fs::absolute(path_from_utf8(exe));
    auto canonical = fs::weakly_canonical(absolute, ec);
    return (ec ? absolute : canonical).parent_path();
}

// The file layout a launcher built without an embedded entry resolves, which is
// how a hand-made bundle ships its application. A packaged launcher carries the
// entry itself and never looks here.
std::vector<fs::path> entry_candidates(const fs::path& dir, const std::string& stem) {
    return {
        dir / (stem + ".int"),
        dir / (stem + ".py"),
        dir / (stem + ".pyw"),
        dir / kStaticEntry,
    };
}

// The programs the launcher runs. A packaged launcher carries both compiled in
// (tools/py_upper generates them); a launcher built by hand has neither and
// resolves its entry from a file next to the executable instead.
const char* embedded_entry() {
#ifdef PY_UPPER_HAS_EMBEDDED_SOURCE
    return py_upper_embedded::kEntry;
#else
    return nullptr;
#endif
}

const char* embedded_smoke() {
#ifdef PY_UPPER_HAS_EMBEDDED_SOURCE
    return py_upper_embedded::kSmoke;
#else
    return nullptr;
#endif
}

// The egg loader and the key it needs. Both are null/empty in a launcher built
// without an egg, which makes the bootstrap skip installation entirely.
const char* embedded_egg_loader() {
#ifdef PY_UPPER_HAS_EMBEDDED_SOURCE
    return py_upper_embedded::kEggLoader;
#else
    return nullptr;
#endif
}

const char* embedded_egg_key() {
#ifdef PY_UPPER_HAS_EMBEDDED_SOURCE
    return py_upper_embedded::kEggKey;
#else
    return "";
#endif
}

const char* embedded_egg_backend() {
#ifdef PY_UPPER_HAS_EMBEDDED_SOURCE
    return py_upper_embedded::kEggBackend;
#else
    return "auto";
#endif
}

// A crash report has to survive the interpreter: the bootstrap writes the
// traceback here before the exception unwinds past PyRun_SimpleString, where a
// SystemExit would otherwise terminate the process before the launcher can act.
fs::path crash_report_path() {
#ifdef _WIN32
    std::wstring buffer(MAX_PATH + 1, L'\0');
    DWORD length = GetTempPathW((DWORD)buffer.size(), buffer.data());
    fs::path directory = length > 0 ? fs::path(buffer.substr(0, length)) : fs::path(L".");
#else
    const char* tmp = std::getenv("TMPDIR");
    fs::path directory = (tmp && *tmp) ? fs::path(tmp) : fs::path("/tmp");
#endif
    return directory / ("py_upper-" + std::to_string(process_id()) + ".log");
}

std::string read_text_file(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) return std::string();
    return std::string((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
}

// Every value that reaches the bootstrap is a Python string literal. A raw
// literal cannot work here: it would preserve a backslash that has to escape
// itself, so a Windows path arrived at Python with every separator doubled.
// UTF-8 bytes above ASCII pass through unescaped; the program text is UTF-8.
std::string py_quote(const std::string& s) {
    std::string r = "'";
    for (unsigned char c : s) {
        switch (c) {
            case '\\': r += "\\\\"; break;
            case '\'': r += "\\'"; break;
            case '\n': r += "\\n"; break;
            case '\r': r += "\\r"; break;
            case '\t': r += "\\t"; break;
            default:
                if (c < 0x20 || c == 0x7f) {
                    char escape[8];
                    std::snprintf(escape, sizeof(escape), "\\x%02x", c);
                    r += escape;
                } else {
                    r += static_cast<char>(c);
                }
        }
    }
    return r + "'";
}

// The bootstrap keeps application-specific Python out of the launcher while
// fixing the two things C++ cannot reach: Python's own streams when there is no
// console, and a traceback that would otherwise be written where nobody looks.
const char* kBootstrapBody = R"PYUPPER(
import os
import runpy
import sys
import traceback


def _py_upper_streams():
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    if os.name == "nt":
        try:
            stream = os.fdopen(
                os.open("CONOUT$", os.O_RDWR | os.O_BINARY),
                "w",
                encoding="utf-8",
                errors="replace",
                buffering=1,
            )
        except OSError:
            stream = None
    if stream is None:
        stream = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _py_upper_report():
    try:
        with open(CRASH_REPORT, "w", encoding="utf-8", errors="replace") as handle:
            traceback.print_exc(file=handle)
    except OSError:
        pass


def _py_upper_run():
    if ENTRY_SOURCE is not None:
        # The entry rides in the launcher, so there is no file to read. The
        # namespace is the one runpy gives a script, and __file__ is a path that
        # exists: the launcher, which is what carries the code.
        namespace = {
            "__name__": "__main__",
            "__file__": ENTRY_FILE,
            "__package__": None,
            "__spec__": None,
            "__builtins__": __builtins__,
        }
        exec(compile(ENTRY_SOURCE, ENTRY_FILE, "exec"), namespace)
    else:
        runpy.run_path(ENTRY_SCRIPT, run_name="__main__")


def _py_upper_install_egg():
    if EGG_LOADER_SOURCE is None or not EGG_PATH or not os.path.isfile(EGG_PATH):
        return
    namespace = {"__name__": "py_upper_eggloader", "__file__": EGG_PATH}
    exec(compile(EGG_LOADER_SOURCE, "<py_upper_eggloader>", "exec"), namespace)
    namespace["install"](EGG_PATH, key_hex=EGG_KEY_HEX, backend=EGG_BACKEND)


_py_upper_streams()
try:
    _py_upper_install_egg()
    _py_upper_run()
except SystemExit:
    raise
except BaseException:
    _py_upper_report()
    raise
)PYUPPER";

// The program the launcher runs arrives from one of two places: compiled into
// the binary, or as a file next to it. The bootstrap below carries whichever
// applies as a literal, so the Python side stays one program with one branch.
std::string bootstrap(const fs::path& script, const fs::path& entry_file, const char* source,
                      const fs::path& egg_path, const char* egg_loader, const char* egg_key,
                      const char* egg_backend, const fs::path& crash_report) {
    std::string text = "ENTRY_FILE = " + py_quote(path_to_utf8(entry_file)) + "\n"
        "ENTRY_SOURCE = None\n"
        "ENTRY_SCRIPT = None\n"
        "CRASH_REPORT = " + py_quote(path_to_utf8(crash_report)) + "\n"
        "EGG_PATH = " + py_quote(path_to_utf8(egg_path)) + "\n"
        "EGG_KEY_HEX = " + py_quote(egg_key ? egg_key : "") + "\n"
        "EGG_BACKEND = " + py_quote(egg_backend ? egg_backend : "auto") + "\n"
        "EGG_LOADER_SOURCE = None\n";
    if (egg_loader != nullptr && *egg_loader != '\0') {
        // Same carrier as the entry: a raw literal whose payload the generator
        // validated, so a triple quote cannot end it early.
        text += std::string("EGG_LOADER_SOURCE = r\"\"\"") + egg_loader + "\"\"\"\n";
    }
    if (source != nullptr) {
        // The generator refuses a payload that carries a triple double quote,
        // which is what keeps this raw literal from ending early
        // (see write_embedded_source in tools/py_upper/package.py).
        text += std::string("ENTRY_SOURCE = r\"\"\"") + source + "\"\"\"\n";
    } else {
        text += "ENTRY_SCRIPT = " + py_quote(path_to_utf8(script)) + "\n";
    }
    return text + kBootstrapBody;
}

bool failed(PyStatus s) {
    if (s._type != 0) {
        if (s.err_msg) std::cerr << "CPython: " << s.err_msg << "\n";
        return true;
    }
    return false;
}

struct RuntimeLoad {
    void* handle{};
    fs::path python_root;
    fs::path stdlib;
    fs::path zip;
    fs::path extension_dir;
    // Why every candidate failed. "Could not load" alone sent debugging down a
    // blind alley more than once; the loader already knows the reason.
    std::string error;
};

// Owns the dlopen/LoadLibrary handle for the whole of PyUpper::run so that every
// exit path — including an exception raised while configuring the interpreter —
// releases the runtime before the process ends.
struct RuntimeGuard {
    void* handle{};
    ~RuntimeGuard() { unload(handle); }
};

RuntimeLoad load_python(const fs::path& home) {
    RuntimeLoad r;
    r.python_root = home;
#ifdef _WIN32
    // The runtime directory holds two kinds of python DLL, and both have to be
    // loaded:
    //   python3XX.dll  the runtime; it exports the entry points the launcher
    //                  resolves.
    //   python3.dll    the stable-ABI forwarder; it re-exports the limited API
    //                  only and pulls python3XX.dll in as a dependency. The
    //                  payload's abi3 extensions (PySide6's QtCore.pyd and
    //                  friends) import it by name, and an import binds to the
    //                  module of that name only while it stays loaded.
    // Resolving from whichever DLL loads first makes a Windows package exit 5:
    // python3.dll loads fine and then answers every GetProcAddress with null.
    std::vector<fs::path> dlls;
    for (const auto& e : fs::directory_iterator(home)) {
        if (e.is_regular_file() && e.path().extension() == ".dll" && path_to_utf8(e.path().filename()).rfind("python", 0) == 0) {
            dlls.push_back(e.path());
        }
    }
    std::sort(dlls.begin(), dlls.end());
    bool loaded_any = false;
    for (const auto& p : dlls) {
        // The loader resolves an implicitly linked dependency (python3.dll
        // pulling python3XX.dll and the CRT) through the standard search order,
        // which does not include the loaded DLL's own directory. Point the
        // search at the runtime directory for this load only.
        void* handle = LoadLibraryExW(W(path_to_utf8(p)).c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (!handle) {
            r.error = path_to_utf8(p) + ": LoadLibraryEx failed with error " + std::to_string(GetLastError());
            continue;
        }
        loaded_any = true;
        if (!r.handle && symbol(handle, kRuntimeEntrySymbol)) r.handle = handle;
    }
    if (!r.handle) {
        std::string reason = loaded_any
            ? "no python DLL in " + path_to_utf8(home) + " exports " + kRuntimeEntrySymbol
            : "no python DLL in " + path_to_utf8(home);
        r.error = r.error.empty() ? reason : reason + "\n" + r.error;
    }
    r.stdlib = home / "Lib";
    // The embedded launcher configures its own module search paths, so the
    // runtime ZIP must not also activate python.org's isolated ._pth handling;
    // with it enabled CPython ignores site-packages entirely.
    for (const auto& e : fs::directory_iterator(home)) {
        auto name = path_to_utf8(e.path().filename());
        if (e.is_regular_file() && name.rfind("python", 0) == 0 && name.find("._pth") != std::string::npos) {
            std::error_code rename_ec;
            fs::rename(e.path(), e.path().string() + ".disabled", rename_ec);
            break;
        }
    }
    // Windows CPython keeps its dynamic stdlib extensions (zlib, _ssl, _socket,
    // ...) in DLLs; without it on the module search path the packaged app
    // cannot import them.
    r.extension_dir = home / "DLLs";
    // python.org's embeddable ZIP places _socket.pyd, _ssl.pyd and other
    // standard extensions directly beside python3XX.dll, unlike PBS's DLLs/.
    if (!fs::exists(r.extension_dir)) r.extension_dir = home;
    for (const auto& e : fs::directory_iterator(home)) {
        if (e.is_regular_file() && e.path().extension() == ".zip" && path_to_utf8(e.path().filename()).rfind("python", 0) == 0) {
            r.zip = e.path();
            break;
        }
    }
#else
    auto libdir = home / "lib";
    if (!fs::exists(libdir)) return r;
    std::vector<fs::path> libs;
    for (const auto& e : fs::directory_iterator(libdir)) {
        auto n = path_to_utf8(e.path().filename());
#if defined(__APPLE__)
        if (e.is_regular_file() && n.rfind("libpython3.", 0) == 0 && e.path().extension() == ".dylib") libs.push_back(e.path());
#else
        if (e.is_regular_file() && n.rfind("libpython3.", 0) == 0 && n.find(".so") != std::string::npos) libs.push_back(e.path());
#endif
    }
    std::sort(libs.begin(), libs.end());
    for (const auto& p : libs) {
        // Unix CPython extensions normally do not link against libpython.
        // When the launcher embeds CPython via dlopen(), libpython symbols
        // must remain globally visible to subsequently imported extensions.
        r.handle = dlopen(p.c_str(), RTLD_NOW | RTLD_GLOBAL);
        if (r.handle) break;
        const char* reason = dlerror();
        r.error = path_to_utf8(p) + ": " + (reason ? reason : "dlopen failed");
    }
    if (!r.handle && r.error.empty()) r.error = "no libpython3.* library in " + path_to_utf8(libdir);
    for (const auto& e : fs::directory_iterator(libdir)) {
        if (e.is_directory() && path_to_utf8(e.path().filename()).rfind("python3.", 0) == 0) {
            r.stdlib = e.path();
            break;
        }
    }
    // A runtime built from a normal CPython/venv keeps dynamic stdlib
    // extensions in lib-dynload, which is a separate sys.path entry.
    if (!r.stdlib.empty()) r.extension_dir = r.stdlib / "lib-dynload";
#endif
    return r;
}

}  // namespace

int PyUpper::run(const std::vector<std::string>& argv) {
    RuntimeGuard runtime;
    FinalizeEx finalize = nullptr;
    IsInitialized initialized = nullptr;
    try {
        if (argv.empty()) {
            report_fatal("Launcher started without an executable path");
            return 1;
        }
        const std::string exe = argv.front();
        const fs::path root = exe_dir(exe);
        const std::string stem = path_to_utf8(path_from_utf8(exe).stem());
        attach_parent_console();

#ifdef _WIN32
        const fs::path home = root / "runtime";
        const fs::path site = root / "site-packages";
        const fs::path entry_dir = root;
#elif defined(__APPLE__)
        const fs::path contents = root.parent_path();
        const fs::path home = contents / "Resources" / "runtime";
        const fs::path site = contents / "Resources" / "site-packages";
        const fs::path entry_dir = contents / "Resources";
#else
        const fs::path home = root / "runtime";
        const fs::path site = root / "site-packages";
        const fs::path entry_dir = root;
#endif

        // The loader embedded in the launcher looks for the archive in the
        // bundle root (Contents/Resources on macOS), which is where packaging
        // copies it. It is simply absent when the project ships loose files.
        const fs::path egg = entry_dir / kEggDir / kEggFile;

        const char* smoke = std::getenv("PY_UPPER_SMOKE");
        const bool smoke_mode = smoke && std::string(smoke) == "1";

        // The program compiled into the launcher wins over a file next to it: a
        // packaged bundle ships no sidecar, and a leftover one from the older
        // layout must not be able to shadow the entry this binary was built
        // with. The file chain is what a hand-made launcher resolves, because
        // that build has nothing embedded to prefer.
        const char* embedded = smoke_mode ? embedded_smoke() : embedded_entry();
        fs::path script;
        if (embedded == nullptr) {
            if (smoke_mode) {
                fs::path candidate = entry_dir / (stem + ".smoke.int");
                if (fs::exists(candidate)) script = candidate;
            } else {
                for (const fs::path& candidate : entry_candidates(entry_dir, stem)) {
                    if (fs::exists(candidate)) {
                        script = candidate;
                        break;
                    }
                }
            }
            if (script.empty()) {
                std::string expected = smoke_mode
                    ? "Expected " + stem + ".smoke.int or a smoke program compiled into the launcher"
                    : "Expected " + stem + ".int, " + stem + ".py, " + stem + ".pyw, " + kStaticEntry
                        + " or an entry compiled into the launcher";
                report_fatal("Entry script missing in " + path_to_utf8(entry_dir) + "\n" + expected);
                return 3;
            }
        }

        // The bundle layout is exported so the application never has to guess its
        // own location by counting parent directories (PyStand exports
        // PYSTAND_HOME for the same reason).
        set_env("PY_UPPER_HOME", path_to_utf8(entry_dir));
        set_env("PY_UPPER_RUNTIME", path_to_utf8(home));
        set_env("PY_UPPER_SITE_PACKAGES", path_to_utf8(site));
        // Exported only when the entry really is a file. A packaged launcher
        // carries its entry, and naming a path that does not exist would be
        // worse than saying nothing.
        if (!script.empty()) set_env("PY_UPPER_SCRIPT", path_to_utf8(script));
        if (fs::exists(egg)) set_env("PY_UPPER_EGG", path_to_utf8(egg));
        set_env("PY_UPPER_EXECUTABLE", exe);
#ifdef PY_UPPER_APP_NAME
        set_env("PY_UPPER_APP_NAME", PY_UPPER_APP_NAME);
#else
        // A launcher built by hand has no compiled-in name; the executable stem
        // is the best answer available.
        set_env("PY_UPPER_APP_NAME", stem);
#endif

        RuntimeLoad loaded = load_python(home);
        runtime.handle = loaded.handle;
        if (!loaded.handle) {
            std::string detail = loaded.error.empty() ? "" : "\n" + loaded.error;
            report_fatal("Could not load the bundled CPython runtime from\n" + path_to_utf8(home) + detail);
            return 4;
        }

        auto init_config = (ConfigInit)symbol(loaded.handle, kRuntimeEntrySymbol);
        auto set_string = (ConfigSetString)symbol(loaded.handle, "PyConfig_SetString");
        auto append = (WideAppend)symbol(loaded.handle, "PyWideStringList_Append");
        auto init = (InitializeFromConfig)symbol(loaded.handle, "Py_InitializeFromConfig");
        auto clear = (ConfigClear)symbol(loaded.handle, "PyConfig_Clear");
        finalize = (FinalizeEx)symbol(loaded.handle, "Py_FinalizeEx");
        initialized = (IsInitialized)symbol(loaded.handle, "Py_IsInitialized");
        if (!init_config || !set_string || !append || !init || !clear || !finalize || !initialized) {
            report_fatal("The bundled CPython runtime is missing required initialization symbols\n" + path_to_utf8(home));
            return 5;
        }

        const fs::path crash_report = crash_report_path();
        std::error_code ec;
        fs::remove(crash_report, ec);

        PyConfig config;
        init_config(&config);
        bool configured = true;
        auto assign = [&](wchar_t** slot, const std::string& value) {
            if (configured) configured = !failed(set_string(&config, slot, W(value).c_str()));
        };

        // The launcher uses an isolated config, so PYTHONDONTWRITEBYTECODE is ignored.
        // A packaged app must not write .pyc caches into its own bundle: that mutates a
        // signed/notarized .app after the fact and breaks its code signature seal.
        config.write_bytecode = 0;
        assign(&config.home, path_to_utf8(home));
        assign(&config.program_name, exe);
        assign(&config.executable, exe);
        if (!configured) {
            clear(&config);
            report_fatal("Could not configure the embedded CPython runtime\n" + path_to_utf8(home));
            return 6;
        }

        config.module_search_paths_set = 1;
        auto add_path = [&](const fs::path& path) {
            if (!configured || path.empty()) return;
            configured = !failed(append(&config.module_search_paths, W(path_to_utf8(path)).c_str()));
        };
        add_path(loaded.zip);
        add_path(loaded.stdlib);
        if (!loaded.extension_dir.empty() && fs::exists(loaded.extension_dir)) add_path(loaded.extension_dir);
        add_path(site);

        config.parse_argv = 0;
        config.argv.length = 0;
        if (configured) configured = !failed(append(&config.argv, W(exe).c_str()));
        for (std::size_t i = 1; configured && i < argv.size(); ++i) {
            configured = !failed(append(&config.argv, W(argv[i]).c_str()));
        }
        if (!configured) {
            clear(&config);
            report_fatal("Could not configure the embedded CPython runtime search path\n" + path_to_utf8(home));
            return 6;
        }

        PyStatus status = init(&config);
        clear(&config);
        if (failed(status) || !initialized()) {
            report_fatal("Could not start the embedded CPython runtime\n" + path_to_utf8(home));
            return 7;
        }

        auto run_simple = (RunSimpleString)symbol(loaded.handle, "PyRun_SimpleString");
        if (!run_simple) {
            finalize();
            finalize = nullptr;
            report_fatal("The bundled CPython runtime is missing PyRun_SimpleString");
            return 8;
        }

        // A compiled-in entry reports the launcher as its own location, a file
        // entry reports the file.
        const fs::path entry_file = script.empty() ? path_from_utf8(exe) : script;
        const std::string source = bootstrap(
            script, entry_file, embedded, egg, embedded_egg_loader(), embedded_egg_key(),
            embedded_egg_backend(), crash_report);
        int rc = run_simple(source.c_str());
        int frc = finalize();
        finalize = nullptr;

        // The bootstrap re-raises an unhandled exception, which makes CPython
        // print the traceback to a stream that may not exist. The report file is
        // the reliable channel, and its presence is what distinguishes a crash
        // from an application that exited non-zero on purpose.
        if (fs::exists(crash_report)) {
            std::string report = read_text_file(crash_report);
            fs::remove(crash_report, ec);
            report_fatal(report.empty() ? "The application terminated with an unhandled exception" : report);
            return 1;
        }
        return rc ? (rc < 0 ? 1 : rc) : frc;
    } catch (const std::exception& e) {
        if (initialized && initialized() && finalize) finalize();
        report_fatal(std::string("Launcher error: ") + e.what());
        return 9;
    }
}
