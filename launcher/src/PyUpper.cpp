#include "PyUpper.h"
#include <Python.h>
#include <filesystem>
#include <iostream>
#include <locale>
#include <codecvt>
#include <vector>
#include <string>
#include <algorithm>
#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
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
std::wstring W(const std::string& s) { int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),(int)s.size(),nullptr,0); if(n<=0) throw std::runtime_error("UTF-8 conversion failed"); std::wstring r(n,L'\\0'); MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),(int)s.size(),r.data(),n); return r; }
void* symbol(void* h,const char* n){return reinterpret_cast<void*>(GetProcAddress((HMODULE)h,n));}
void unload(void* h){if(h) FreeLibrary((HMODULE)h);}
#else
std::wstring W(const std::string& s){return std::wstring_convert<std::codecvt_utf8<wchar_t>>().from_bytes(s);}
void* symbol(void* h,const char* n){return dlsym(h,n);}
void unload(void* h){if(h) dlclose(h);}
#endif
fs::path exe_dir(const std::string& exe){return fs::absolute(fs::path(exe)).parent_path();}

struct RuntimeLoad { void* handle{}; fs::path python_root; fs::path stdlib; fs::path zip; };
RuntimeLoad load_python(const fs::path& home) {
    RuntimeLoad r; r.python_root=home;
#ifdef _WIN32
    std::vector<fs::path> dlls;
    if (fs::exists(home/"python3.dll")) dlls.push_back(home/"python3.dll");
    for (const auto& e: fs::directory_iterator(home)) if (e.is_regular_file() && e.path().extension()==".dll" && e.path().filename().string().rfind("python",0)==0) dlls.push_back(e.path());
    for (const auto& p: dlls) { r.handle=LoadLibraryW(W(p.string()).c_str()); if(r.handle) break; }
    r.stdlib=home/"Lib";
    for (const auto& e: fs::directory_iterator(home)) if(e.is_regular_file() && e.path().extension()==".zip" && e.path().filename().string().rfind("python",0)==0){r.zip=e.path();break;}
#else
    auto libdir=home/"lib";
    if (!fs::exists(libdir)) return r;
    std::vector<fs::path> libs;
    for (const auto& e: fs::directory_iterator(libdir)) {
        auto n=e.path().filename().string();
#if defined(__APPLE__)
        if(e.is_regular_file() && n.rfind("libpython3.",0)==0 && e.path().extension()==".dylib") libs.push_back(e.path());
#else
        if(e.is_regular_file() && n.rfind("libpython3.",0)==0 && n.find(".so")!=std::string::npos) libs.push_back(e.path());
#endif
    }
    std::sort(libs.begin(),libs.end());
    for(const auto& p:libs){r.handle=dlopen(p.c_str(),RTLD_NOW|RTLD_LOCAL);if(r.handle)break;}
    for(const auto& e:fs::directory_iterator(libdir)) if(e.is_directory() && e.path().filename().string().rfind("python3.",0)==0){r.stdlib=e.path();break;}
#endif
    return r;
}
std::string py_quote(const std::string& s){std::string r="r'";for(char c:s){if(c=='\\')r+="\\\\";else if(c=='\'')r+="\\'";else if(c=='\n')r+="\\n";else if(c=='\r')r+="\\r";else r+=c;}return r+"'";}
bool failed(PyStatus s){if(s._type!=0){if(s.err_msg)std::cerr<<"CPython: "<<s.err_msg<<"\n";return true;}return false;}
}

int PyUpper::run(const std::string& exe,const std::vector<std::string>& args){
    const auto root=exe_dir(exe);
    const auto app_stem=fs::path(exe).stem().string();
#ifdef _WIN32
    const auto home=root/"runtime"; const auto site=root/"site-packages"; const auto script=root/(app_stem+".int");
#elif defined(__APPLE__)
    const auto contents=root.parent_path(); const auto home=contents/"Resources"/"runtime"; const auto site=contents/"Resources"/"site-packages"; const auto script=contents/"Resources"/(app_stem+".int");
#else
    const auto home=root/"runtime"; const auto site=root/"site-packages"; const auto script=root/(app_stem+".int");
#endif
    if(!fs::exists(script)){std::cerr<<"Entry script missing: "<<script<<"\n";return 3;}
    auto loaded=load_python(home); if(!loaded.handle){std::cerr<<"Could not load bundled CPython from "<<home<<"\n";return 4;}
    auto init_config=(ConfigInit)symbol(loaded.handle,"PyConfig_InitIsolatedConfig"); auto set_string=(ConfigSetString)symbol(loaded.handle,"PyConfig_SetString"); auto append=(WideAppend)symbol(loaded.handle,"PyWideStringList_Append"); auto init=(InitializeFromConfig)symbol(loaded.handle,"Py_InitializeFromConfig"); auto clear=(ConfigClear)symbol(loaded.handle,"PyConfig_Clear"); auto finalize=(FinalizeEx)symbol(loaded.handle,"Py_FinalizeEx"); auto initialized=(IsInitialized)symbol(loaded.handle,"Py_IsInitialized");
    if(!init_config||!set_string||!append||!init||!clear||!finalize||!initialized){std::cerr<<"Required CPython initialization symbols are missing\n";unload(loaded.handle);return 5;}
    try{
        PyConfig config; init_config(&config); auto whome=W(home.string()); auto wexe=W(exe);
        auto status=set_string(&config,&config.home,whome.c_str()); if(failed(status)){clear(&config);unload(loaded.handle);return 6;}
        status=set_string(&config,&config.program_name,wexe.c_str()); if(failed(status)){clear(&config);unload(loaded.handle);return 6;}
        status=set_string(&config,&config.executable,wexe.c_str()); if(failed(status)){clear(&config);unload(loaded.handle);return 6;}
        config.module_search_paths_set=1;
        if(!loaded.zip.empty()){auto wzip=W(loaded.zip.string());status=append(&config.module_search_paths,wzip.c_str());if(failed(status)){clear(&config);unload(loaded.handle);return 6;}}
        if(!loaded.stdlib.empty()){auto wlib=W(loaded.stdlib.string());status=append(&config.module_search_paths,wlib.c_str());if(failed(status)){clear(&config);unload(loaded.handle);return 6;}}
        auto wsite=W(site.string());status=append(&config.module_search_paths,wsite.c_str());if(failed(status)){clear(&config);unload(loaded.handle);return 6;}
        config.parse_argv=0; config.argv.length=0; status=append(&config.argv,wexe.c_str());if(failed(status)){clear(&config);unload(loaded.handle);return 6;}
        for(const auto& arg:args){auto warg=W(arg);status=append(&config.argv,warg.c_str());if(failed(status)){clear(&config);unload(loaded.handle);return 6;}}
        status=init(&config);clear(&config);if(failed(status)||!initialized()){unload(loaded.handle);return 7;}
        using RunSimpleString=int(*)(const char*); auto run_simple=(RunSimpleString)symbol(loaded.handle,"PyRun_SimpleString"); if(!run_simple){finalize();unload(loaded.handle);return 8;}
        int rc=run_simple((std::string("import runpy\nrunpy.run_path(")+py_quote(script.string())+", run_name='__main__')\n").c_str()); int frc=finalize();unload(loaded.handle);return rc?rc:frc;
    }catch(const std::exception&e){std::cerr<<"Launcher error: "<<e.what()<<"\n";if(initialized())finalize();unload(loaded.handle);return 9;}
}
