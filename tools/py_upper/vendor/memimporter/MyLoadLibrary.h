/**
 * @file MyLoadLibrary.h
 * @brief 声明混合 DLL 加载入口；参数和句柄类型必须与 C 实现保持 ABI 一致。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#ifndef GENERALLOADLIBRARY_H
#define GENERALLOADLIBRARY_H

HMODULE MyLoadLibrary(LPCSTR, void *, size_t, void *);

HMODULE MyGetModuleHandle(LPCSTR);

BOOL MyFreeLibrary(HMODULE);

FARPROC MyGetProcAddress(HMODULE, LPCSTR);


#endif
