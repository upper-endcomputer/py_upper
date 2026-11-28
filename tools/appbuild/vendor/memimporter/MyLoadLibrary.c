/**
 * @file MyLoadLibrary.c
 * @brief 把系统 DLL 与内存 DLL 统一为加载接口；链表引用计数负责共享模块的释放时机。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#include <Python.h>
#include <windows.h>

#include "MemoryModule.h"
#include "MyLoadLibrary.h"

// #define VERBOSE /* enable to print debug output */

/*

Windows API:
============

HMODULE LoadLibraryA(LPCSTR)
HMODULE GetModuleHandleA(LPCSTR)
BOOL FreeLibrary(HMODULE)
FARPROC GetProcAddress(HMODULE, LPCSTR)
BOOL GetModuleHandleExW(DWORD, LPCWSTR, HMODULE *);

MemoryModule API:
=================

HMEMORYMODULE MemoryLoadLibrary(void *)
void MemoryFreeLibrary(HMEMORYMODULE)
FARPROC MemoryGetProcAddress(HMEMORYMODULE, LPCSTR)

HMEMORYMODULE MemoryLoadLibraryEx(void *,
                                 load_func, getproc_func, free_func, userdata)

(there are also some resource functions which are not used here...)

General API in this file:
=========================

HMODULE MyLoadLibrary(LPCSTR, void *, userdata)
HMODULE MyGetModuleHandle(LPCSTR)
BOOL MyFreeLibrary(HMODULE)
FARPROC MyGetProcAddress(HMODULE, LPCSTR)
BOOL WINAPI MyGetModuleHandleExW(DWORD, LPCWSTR, HMODULE *)

*/

/****************************************************************
 * A linked list of loaded MemoryModules.
 */
typedef struct tagLIST {
	HCUSTOMMODULE module;
	char *name;
	struct tagLIST *next;
	struct tagLIST *prev;
	int refcount;
} LIST;

static LIST *libraries;

int level;

/** @brief 在 VERBOSE 构建下输出带递归层级缩进的加载日志。 */
static int dprintf(char *fmt, ...)
{
#ifdef VERBOSE
	va_list marker;
	int i;

	va_start(marker, fmt);
	for (i = 0; i < level; ++i) {
		putchar(' ');
		putchar(' ');
	}
	int result = vfprintf(stderr, fmt, marker) + 2*level;
	va_end(marker);
	return result;
#else
	return 0;
#endif
}

#define PUSH() level++
#define POP()  level--

/****************************************************************
 * Search for a loaded MemoryModule in the linked list, either by name
 * or by module handle.
 */
/** @brief 按不区分大小写的名称或句柄查找已登记内存模块；返回借用节点。 */
static LIST *_FindMemoryModule(LPCSTR name, HMODULE module)
{
	LIST *lib = libraries;
	while (lib) {
		if (name && 0 == _stricmp(name, lib->name)) {
			dprintf("_FindMemoryModule(%s, %p) -> %s[%d]\n", name, module, lib->name, lib->refcount);
			return lib;
		} else if (module == lib->module) {
			dprintf("_FindMemoryModule(%s, %p) -> %s[%d]\n", name, module, lib->name, lib->refcount);
			return lib;
		} else {
			lib = lib->next;
		}
	}
	return NULL;
}

/****************************************************************
 * Insert a MemoryModule into the linked list of loaded modules
 */
/** @brief 分配并登记内存模块节点，初始引用计数为一；节点由最终释放路径回收。 */
static LIST *_AddMemoryModule(LPCSTR name, HCUSTOMMODULE module)
{
	LIST *entry = (LIST *)malloc(sizeof(LIST));
	if (entry == NULL) {
		SetLastError(ERROR_OUTOFMEMORY);
		return NULL;
	}
	entry->name = _strdup(name);
	if (entry->name == NULL) {
		free(entry);
		SetLastError(ERROR_OUTOFMEMORY);
		return NULL;
	}
	entry->module = module;
	entry->next = libraries;
	entry->prev = NULL;
	entry->refcount = 1;
	if (libraries) libraries->prev = entry;
	libraries = entry;
	dprintf("_AddMemoryModule(%s, %p) -> %p[%d]\n",
		name, module, entry, entry->refcount);
	return entry;
}

/****************************************************************
 * Helper functions for MemoryLoadLibraryEx
 */
/** @brief 适配 MemoryModule 回调接口，将符号查找转给统一解析器。 */
static FARPROC _GetProcAddress(HCUSTOMMODULE module, LPCSTR name, void *userdata)
{
	return MyGetProcAddress(module, name);
}

/** @brief 适配依赖释放回调，递减统一模块引用计数。 */
static void _FreeLibrary(HCUSTOMMODULE module, void *userdata)
{
	MyFreeLibrary(module);
}

/** @brief 调用 Python 成员读取器；返回新的 Python 引用，调用者负责 DECREF。 */
PyObject *CallFindproc(PyObject *findproc, LPCSTR filename)
{
	PyObject *res = NULL;
	PyObject *args;
	PyObject *name = PyUnicode_FromString(filename);
	if (name == NULL) return NULL;
	args = PyTuple_Pack(1, name);
	Py_DECREF(name);
	if (args == NULL)
		return NULL;
	res = PyObject_CallObject(findproc, args);
	Py_DECREF(args);
	return res;
}

/** @brief 先复用内存模块，再尝试归档成员，最后回退系统 DLL 加载。 */
static HCUSTOMMODULE _LoadLibrary(LPCSTR filename, void *userdata)
{
	HCUSTOMMODULE result;
	LIST *lib;
	dprintf("_LoadLibrary(%s, %p)\n", filename, userdata);
	PUSH();
	lib = _FindMemoryModule(filename, NULL);
	if (lib) {
		lib->refcount += 1;
		POP();
		dprintf("_LoadLibrary(%s, %p) -> %s[%d]\n\n",
			filename, userdata, lib->name, lib->refcount);
		return lib->module;
	}
	if (userdata) {
		PyObject *findproc = (PyObject *)userdata;
		// Since we are using the Py_LIMITED_API with dynamic loading
		// we would have to implement PyObject_CallFunction() ourselves,
		// which would be a paint since there is no PyObject_VaCallFunction.
		//
		// So we implement a special CallFindproc function
		// which encapsulates the dance we have to do.
//		PyObject *res = PyObject_CallFunction(findproc, "s", filename);
		PyObject *res = CallFindproc(findproc, filename);
		if (res && PyBytes_AsString(res)) {
			result = MemoryLoadLibraryEx(PyBytes_AsString(res), PyBytes_GET_SIZE(res),
				MemoryDefaultAlloc, MemoryDefaultFree,
				_LoadLibrary, _GetProcAddress, _FreeLibrary,
				userdata);
			Py_DECREF(res);
			if (result) {
				lib = _AddMemoryModule(filename, result);
				if (!lib) {
					MemoryFreeLibrary(result);
					POP();
					return NULL;
				}
				POP();
				dprintf("_LoadLibrary(%s, %p) -> %p %s[%d]\n\n",
					filename, userdata, lib->module, lib->name, lib->refcount);
				return lib->module;
			} else {
				dprintf("_LoadLibrary(%s, %p) failed with error %d\n",
					filename, userdata, GetLastError());
			}
		} else {
			Py_XDECREF(res);
			PyErr_Clear();
		}
	}
	SetLastError(0);
	result = (HCUSTOMMODULE)LoadLibraryA(filename);
	POP();
	dprintf("LoadLibraryA(%s) -> %p\n\n", filename, result);
	return result;
}

/****************************************************************
 * Public functions
 */
/** @brief 只查询已登记内存模块；返回借用句柄，不增加引用计数。 */
HMODULE MyGetModuleHandle(LPCSTR name)
{
	LIST *lib;
	lib = _FindMemoryModule(name, NULL);
	if (lib)
		return lib->module;
	SetLastError(0);
    return NULL;
	//return GetModuleHandle(name);
}

/** @brief 按读取回调或字节缓冲区加载模块；返回持有引用的句柄，须与 MyFreeLibrary 配对。 */
HMODULE MyLoadLibrary(LPCSTR name, void *bytes, size_t size, void *userdata)
{
	if (userdata) {
		HCUSTOMMODULE mod = _LoadLibrary(name, userdata);
		if (mod)
			return mod;
	} else if (bytes) {
		HCUSTOMMODULE mod = MemoryLoadLibraryEx(bytes, size,
							MemoryDefaultAlloc, MemoryDefaultFree,
							_LoadLibrary,
							_GetProcAddress,
							_FreeLibrary,
							userdata);
		if (mod) {
			LIST *lib = _AddMemoryModule(name, mod);
			if (lib) return lib->module;
			MemoryFreeLibrary(mod);
			return NULL;
		}
	}
	return LoadLibraryA(name);
}

/** @brief 递减内存模块引用并在归零时卸载；系统模块交给 FreeLibrary。 */
BOOL MyFreeLibrary(HMODULE module)
{
	LIST *lib = _FindMemoryModule(NULL, module);
	if (lib) {
		if (--lib->refcount == 0) {
			/* 先摘链再递归卸载依赖，避免回调查询到已释放模块。 */
			if (lib->prev) lib->prev->next = lib->next;
			else libraries = lib->next;
			if (lib->next) lib->next->prev = lib->prev;
			MemoryFreeLibrary(module);
			free(lib->name);
			free(lib);
		}
		return TRUE;
	} else {
		SetLastError(0);
		return FreeLibrary(module);
	}
}

/** @brief 适配内存扩展的地址查询场景；其余标志交给系统 API。 */
BOOL WINAPI MyGetModuleHandleExW(DWORD flags, LPCWSTR modname, HMODULE *pmodule)
{
	if (flags & GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS && pmodule != NULL) {
		*pmodule = GetModuleHandle(NULL);
		return TRUE;
	}
	return GetModuleHandleExW(flags, modname, pmodule);
}

/** @brief 解析内存或系统模块导出，并为 GetModuleHandleExW 提供适配入口。 */
FARPROC MyGetProcAddress(HMODULE module, LPCSTR procname)
{
	FARPROC proc;
	LIST *lib = _FindMemoryModule(NULL, module);
	if (lib)
		proc = MemoryGetProcAddress(lib->module, procname);
	else {
		SetLastError(0);
		proc = GetProcAddress(module, procname);
		if (proc == (FARPROC)&GetModuleHandleExW)
			proc = (FARPROC)MyGetModuleHandleExW;
	}
	return proc;
}
