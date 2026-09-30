#include "PyStand.h"
#include <Python.h>
#include <filesystem>
#include <iostream>
#include <locale>
#include <codecvt>
#include <vector>
#include <string>
#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#include <cstdlib>
#endif

namespace fs = std::filesystem;
namespace {

using ConfigInit = void (*)(PyConfig*);
using ConfigSetString = PyStatus (*)(PyConfig*, wchar_t**, const wchar_t*);
using WideAppend = PyStatus (*)(PyWideStringList*, const wchar_t*);
using InitializeFromConfig = PyStatus (*)(const PyConfig*);
using ConfigClear = void (*)(PyConfig*);
using FinalizeEx = int (*)();
using IsInitialized = int (*)();

#ifdef _WIN32
std::wstring W(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int)s.size(), nullptr, 0);
    if (n <= 0) throw std::runtime_error("UTF-8 to UTF-16 conversion failed");
    std::wstring r(n, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int)s.size(), r.data(), n);
    return r;
}
#else
std::wstring W(const std::string& s) {
    return std::wstring_convert<std::codecvt_utf8<wchar_t>>().from_bytes(s);
}
#endif

fs::path exe_dir(const std::string& exe) {
    return fs::absolute(fs::path(exe)).parent_path();
}

#ifdef _WIN32
void* load_python(const fs::path& home) {
    const auto candidates = {home / "python3.dll", home / "python313.dll"};
    for (const auto& p : candidates) {
        HMODULE h = LoadLibraryW(W(p.string()).c_str());
        if (h) return h;
    }
    return nullptr;
}
void* symbol(void* h, const char* name) { return reinterpret_cast<void*>(GetProcAddress((HMODULE)h, name)); }
void unload_python(void* h) { if (h) FreeLibrary((HMODULE)h); }
#else
void* load_python(const fs::path& home) {
    auto libdir = home / "lib";
    if (!fs::exists(libdir)) return nullptr;
    for (const auto& e : fs::directory_iterator(libdir)) {
        auto n = e.path().filename().string();
#if defined(__APPLE__)
        if (n.rfind("libpython3.13", 0) == 0 && e.path().extension() == ".dylib")
#else
        if (n.rfind("libpython3.13", 0) == 0 && n.find(".so") != std::string::npos)
#endif
        {
            return dlopen(e.path().c_str(), RTLD_NOW | RTLD_LOCAL);
        }
    }
    return nullptr;
}
void* symbol(void* h, const char* name) { return dlsym(h, name); }
void unload_python(void* h) { if (h) dlclose(h); }
#endif

std::string py_quote(const std::string& s) {
    std::string r = "r\'";
    for (char c : s) {
        if (c == '\\' ) r += "\\\\";
        else if (c == '\'') r += "\\\'";
        else if (c == '\n') r += "\\n";
        else if (c == '\r') r += "\\r";
        else r += c;
    }
    r += "\'";
    return r;
}

bool failed(PyStatus status) {
    if (status._type != 0) {
        if (status.err_msg) std::cerr << "CPython: " << status.err_msg << "\n";
        return true;
    }
    return false;
}

} // namespace

int PyStand::run(const std::string& exe, const std::vector<std::string>& args) {
    const auto root = exe_dir(exe);
#ifdef _WIN32
    const auto contents = root;
    const auto home = contents / "runtime";
    const auto site = contents / "site-packages";
    const auto script = contents / "MyApp.int";
#elif defined(__APPLE__)
    const auto contents = root.parent_path();
    const auto home = contents / "Resources" / "runtime";
    const auto site = contents / "Resources" / "site-packages";
    const auto script = contents / "Resources" / "MyApp.int";
#else
    const auto contents = root;
    const auto home = contents / "runtime";
    const auto site = contents / "site-packages";
    const auto script = contents / "MyApp.int";
#endif

    if (!fs::exists(script)) {
        std::cerr << "Entry script missing: " << script << "\n";
        return 3;
    }

    void* handle = load_python(home);
    if (!handle) {
        std::cerr << "Could not load bundled CPython from " << home << "\n";
        return 4;
    }

    auto init_config = reinterpret_cast<ConfigInit>(symbol(handle, "PyConfig_InitIsolatedConfig"));
    auto set_string = reinterpret_cast<ConfigSetString>(symbol(handle, "PyConfig_SetString"));
    auto append_path = reinterpret_cast<WideAppend>(symbol(handle, "PyWideStringList_Append"));
    auto init = reinterpret_cast<InitializeFromConfig>(symbol(handle, "Py_InitializeFromConfig"));
    auto clear = reinterpret_cast<ConfigClear>(symbol(handle, "PyConfig_Clear"));
    auto finalize = reinterpret_cast<FinalizeEx>(symbol(handle, "Py_FinalizeEx"));
    auto initialized = reinterpret_cast<IsInitialized>(symbol(handle, "Py_IsInitialized"));

    if (!init_config || !set_string || !append_path || !init || !clear || !finalize || !initialized) {
        std::cerr << "Required CPython 3.13 initialization symbols are missing\n";
        unload_python(handle);
        return 5;
    }

    try {
        PyConfig config;
        init_config(&config);

        const auto whome = W(home.string());
        const auto wexe = W(exe);
        auto status = set_string(&config, &config.home, whome.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }
        status = set_string(&config, &config.program_name, wexe.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }
        status = set_string(&config, &config.executable, wexe.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }

        config.module_search_paths_set = 1;
#ifdef _WIN32
        const auto wzip = W((home / "python313.zip").string());
        const auto wlib = W((home / "Lib").string());
#else
        const auto wlib = W((home / "lib" / "python3.13").string());
#endif
        const auto wsite = W(site.string());
#ifdef _WIN32
        status = append_path(&config.module_search_paths, wzip.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }
#endif
        status = append_path(&config.module_search_paths, wlib.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }
        status = append_path(&config.module_search_paths, wsite.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }

        config.parse_argv = 0;
        config.argv.length = 0;
        const auto wscript = W(script.string());
        status = append_path(&config.argv, wexe.c_str());
        if (failed(status)) { clear(&config); unload_python(handle); return 6; }
        for (const auto& arg : args) {
            auto warg = W(arg);
            status = append_path(&config.argv, warg.c_str());
            if (failed(status)) { clear(&config); unload_python(handle); return 6; }
        }

        status = init(&config);
        clear(&config);
        if (failed(status) || !initialized()) {
            unload_python(handle);
            return 7;
        }

        std::string escaped = script.string();
        std::string code = "import runpy\nrunpy.run_path(" + py_quote(escaped) + ", run_name='__main__')\n";

        // PyRun_SimpleString is intentionally resolved after initialization.
        using RunSimpleString = int (*)(const char*);
        auto run_simple = reinterpret_cast<RunSimpleString>(symbol(handle, "PyRun_SimpleString"));
        if (!run_simple) {
            finalize();
            unload_python(handle);
            return 8;
        }
        const int rc = run_simple(code.c_str());
        const int frc = finalize();
        unload_python(handle);
        return rc ? rc : frc;
    } catch (const std::exception& e) {
        std::cerr << "Launcher error: " << e.what() << "\n";
        if (initialized()) finalize();
        unload_python(handle);
        return 9;
    }
}
