#include "PyUpper.h"
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

// The entry file that survives renaming the executable, so a renamed launcher
// still finds its application (PyStand's _pystand_static.int).
constexpr const char* kStaticEntry = "_py_upper_static.int";
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
    // argv[0] can be relative ("./MyApp"), which would leave a "." component
    // in the parent chain and misplace the bundle root. Normalise first.
    std::error_code ec;
    auto absolute = fs::absolute(path_from_utf8(exe), ec);
    if (ec) absolute = fs::absolute(path_from_utf8(exe));
    auto canonical = fs::weakly_canonical(absolute, ec);
    return (ec ? absolute : canonical).parent_path();
}

// The executable-derived name is the documented layout; the extra suffixes and
// the static name cover hand-made bundles and a renamed executable.
std::vector<fs::path> entry_candidates(const fs::path& dir, const std::string& stem) {
    return {
        dir / (stem + ".int"),
        dir / (stem + ".py"),
        dir / (stem + ".pyw"),
        dir / kStaticEntry,
    };
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

std::string py_quote(const std::string& s) {
    std::string r = "r'";
    for (char c : s) {
        if (c == '\\') r += "\\\\";
        else if (c == '\'') r += "\\'";
        else if (c == '\n') r += "\\n";
        else if (c == '\r') r += "\\r";
        else r += c;
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


_py_upper_streams()
try:
    runpy.run_path(ENTRY_SCRIPT, run_name="__main__")
except SystemExit:
    raise
except BaseException:
    _py_upper_report()
    raise
)PYUPPER";

std::string bootstrap(const fs::path& script, const fs::path& crash_report) {
    return "ENTRY_SCRIPT = " + py_quote(path_to_utf8(script)) + "\n"
        + "CRASH_REPORT = " + py_quote(path_to_utf8(crash_report)) + "\n"
        + kBootstrapBody;
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
    std::vector<fs::path> dlls;
    if (fs::exists(home / "python3.dll")) dlls.push_back(home / "python3.dll");
    for (const auto& e : fs::directory_iterator(home)) {
        if (e.is_regular_file() && e.path().extension() == ".dll" && path_to_utf8(e.path().filename()).rfind("python", 0) == 0) {
            dlls.push_back(e.path());
        }
    }
    for (const auto& p : dlls) {
        // The loader resolves an implicitly linked dependency (python3.dll
        // pulling python3XX.dll and the CRT) through the standard search order,
        // which does not include the loaded DLL's own directory. Point the
        // search at the runtime directory for this load only.
        r.handle = LoadLibraryExW(W(path_to_utf8(p)).c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (r.handle) break;
    }
    r.stdlib = home / "Lib";
    // Windows CPython keeps its dynamic stdlib extensions (zlib, _ssl, _socket,
    // ...) in DLLs; without it on the module search path the packaged app
    // cannot import them.
    r.extension_dir = home / "DLLs";
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
    }
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

        const char* smoke = std::getenv("PY_UPPER_SMOKE");
        const bool smoke_mode = smoke && std::string(smoke) == "1";

        fs::path script;
        if (smoke_mode) {
            script = entry_dir / (stem + ".smoke.int");
        } else {
            for (const fs::path& candidate : entry_candidates(entry_dir, stem)) {
                if (fs::exists(candidate)) {
                    script = candidate;
                    break;
                }
            }
        }
        if (script.empty()) {
            report_fatal("Entry script missing in " + path_to_utf8(entry_dir) + "\nExpected " + stem + ".int, " + stem + ".py, " + stem + ".pyw or " + kStaticEntry);
            return 3;
        }

        // The bundle layout is exported so the application never has to guess its
        // own location by counting parent directories (PyStand exports
        // PYSTAND_HOME for the same reason).
        set_env("PY_UPPER_HOME", path_to_utf8(entry_dir));
        set_env("PY_UPPER_RUNTIME", path_to_utf8(home));
        set_env("PY_UPPER_SITE_PACKAGES", path_to_utf8(site));
        set_env("PY_UPPER_SCRIPT", path_to_utf8(script));
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
            report_fatal("Could not load the bundled CPython runtime from\n" + path_to_utf8(home));
            return 4;
        }

        auto init_config = (ConfigInit)symbol(loaded.handle, "PyConfig_InitIsolatedConfig");
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

        const std::string source = bootstrap(script, crash_report);
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
