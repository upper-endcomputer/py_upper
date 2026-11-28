/**
 * @file MemoryModule.h
 * @brief 声明内存 PE 加载器句柄和回调；加载与释放必须使用配对接口，遵守上游许可。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
/*
 * Memory DLL loading code
 * Version 0.0.4
 *
 * Copyright (c) 2004-2015 by Joachim Bauch / mail@joachim-bauch.de
 * http://www.joachim-bauch.de
 *
 * The contents of this file are subject to the Mozilla Public License Version
 * 2.0 (the "License"); you may not use this file except in compliance with
 * the License. You may obtain a copy of the License at
 * http://www.mozilla.org/MPL/
 *
 * Software distributed under the License is distributed on an "AS IS" basis,
 * WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
 * for the specific language governing rights and limitations under the
 * License.
 *
 * The Original Code is MemoryModule.h
 *
 * The Initial Developer of the Original Code is Joachim Bauch.
 *
 * Portions created by Joachim Bauch are Copyright (C) 2004-2015
 * Joachim Bauch. All Rights Reserved.
 *
 */

#ifndef __MEMORY_MODULE_HEADER
#define __MEMORY_MODULE_HEADER

#include <windows.h>

typedef void *HMEMORYMODULE;

typedef void *HMEMORYRSRC;

typedef void *HCUSTOMMODULE;

#ifdef __cplusplus
extern "C" {
#endif

typedef LPVOID (*CustomAllocFunc)(LPVOID, SIZE_T, DWORD, DWORD, void*);
typedef BOOL (*CustomFreeFunc)(LPVOID, SIZE_T, DWORD, void*);
typedef HCUSTOMMODULE (*CustomLoadLibraryFunc)(LPCSTR, void *);
typedef FARPROC (*CustomGetProcAddressFunc)(HCUSTOMMODULE, LPCSTR, void *);
typedef void (*CustomFreeLibraryFunc)(HCUSTOMMODULE, void *);

/**
 * Load EXE/DLL from memory location with the given size.
 *
 * All dependencies are resolved using default LoadLibrary/GetProcAddress
 * calls through the Windows API.
 */
/** @brief 使用默认分配和依赖回调映射 PE；成功句柄须由 MemoryFreeLibrary 释放。 */
HMEMORYMODULE MemoryLoadLibrary(const void *, size_t);

/**
 * Load EXE/DLL from memory location with the given size using custom dependency
 * resolvers.
 *
 * Dependencies will be resolved using passed callback methods.
 */
/** @brief 按提供的回调映射 PE、重定位、解析导入并初始化；输入字节仅在加载期间借用。 */
HMEMORYMODULE MemoryLoadLibraryEx(const void *, size_t,
    CustomAllocFunc,
    CustomFreeFunc,
    CustomLoadLibraryFunc,
    CustomGetProcAddressFunc,
    CustomFreeLibraryFunc,
    void *);

/**
 * Get address of exported method. Supports loading both by name and by
 * ordinal value.
 */
/** @brief 按名称或序号查找导出；返回地址在模块卸载后失效。 */
FARPROC MemoryGetProcAddress(HMEMORYMODULE, LPCSTR);

/**
 * Free previously loaded EXE/DLL.
 */
/** @brief 通知 DLL 分离并释放导入句柄、映射内存及管理结构；空句柄可安全传入。 */
void MemoryFreeLibrary(HMEMORYMODULE);

/**
 * Execute entry point (EXE only). The entry point can only be executed
 * if the EXE has been loaded to the correct base address or it could
 * be relocated (i.e. relocation information have not been stripped by
 * the linker).
 *
 * Important: calling this function will not return, i.e. once the loaded
 * EXE finished running, the process will terminate.
 *
 * Returns a negative value if the entry point could not be executed.
 */
/** @brief 调用已重定位 EXE 入口；DLL、空入口或无效模块返回 -1。 */
int MemoryCallEntryPoint(HMEMORYMODULE);

/**
 * Find the location of a resource with the specified type and name.
 */
/** @brief 按默认语言查询资源，返回依赖模块存活期的借用资源句柄。 */
HMEMORYRSRC MemoryFindResource(HMEMORYMODULE, LPCTSTR, LPCTSTR);

/**
 * Find the location of a resource with the specified type, name and language.
 */
/** @brief 按类型、名称和语言遍历资源树；缺少请求语言时选择可用语言。 */
HMEMORYRSRC MemoryFindResourceEx(HMEMORYMODULE, LPCTSTR, LPCTSTR, WORD);

/**
 * Get the size of the resource in bytes.
 */
/** @brief 返回资源字节长度；空资源句柄返回零。 */
DWORD MemorySizeofResource(HMEMORYMODULE, HMEMORYRSRC);

/**
 * Get a pointer to the contents of the resource.
 */
/** @brief 返回映像内资源数据借用指针；不可单独释放，模块卸载后失效。 */
LPVOID MemoryLoadResource(HMEMORYMODULE, HMEMORYRSRC);

/**
 * Load a string resource.
 */
/** @brief 按默认语言复制资源字符串；缓冲区和容量由调用者提供。 */
int MemoryLoadString(HMEMORYMODULE, UINT, LPTSTR, int);

/**
 * Load a string resource with a given language.
 */
/** @brief 按语言复制最多 maxsize 个字符；缓冲区恰满时不保证追加终止符。 */
int MemoryLoadStringEx(HMEMORYMODULE, UINT, LPTSTR, int, WORD);

/**
* Default implementation of CustomAllocFunc that calls VirtualAlloc
* internally to allocate memory for a library
*
* This is the default as used by MemoryLoadLibrary.
*/
/** @brief 默认内存分配回调，转交 VirtualAlloc；大小单位为字节。 */
LPVOID MemoryDefaultAlloc(LPVOID, SIZE_T, DWORD, DWORD, void *);

/**
* Default implementation of CustomFreeFunc that calls VirtualFree
* internally to free the memory used by a library
*
* This is the default as used by MemoryLoadLibrary.
*/
/** @brief 默认内存释放回调，转交 VirtualFree。 */
BOOL MemoryDefaultFree(LPVOID, SIZE_T, DWORD, void *);

/**
 * Default implementation of CustomLoadLibraryFunc that calls LoadLibraryA
 * internally to load an additional libary.
 *
 * This is the default as used by MemoryLoadLibrary.
 */
/** @brief 默认依赖加载回调；返回系统库句柄，交由配套 free 回调释放。 */
HCUSTOMMODULE MemoryDefaultLoadLibrary(LPCSTR, void *);

/**
 * Default implementation of CustomGetProcAddressFunc that calls GetProcAddress
 * internally to get the address of an exported function.
 *
 * This is the default as used by MemoryLoadLibrary.
 */
/** @brief 默认符号解析回调，返回借用函数地址。 */
FARPROC MemoryDefaultGetProcAddress(HCUSTOMMODULE, LPCSTR, void *);

/**
 * Default implementation of CustomFreeLibraryFunc that calls FreeLibrary
 * internally to release an additional libary.
 *
 * This is the default as used by MemoryLoadLibrary.
 */
/** @brief 释放默认加载器持有的系统库引用。 */
void MemoryDefaultFreeLibrary(HCUSTOMMODULE, void *);

#ifdef __cplusplus
}
#endif

#endif  // __MEMORY_MODULE_HEADER
