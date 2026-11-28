/**
 * @file MyLoadLibrary.h
 * @brief 声明混合 DLL 加载入口；参数和句柄类型必须与 C 实现保持 ABI 一致。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#ifndef GENERALLOADLIBRARY_H
#define GENERALLOADLIBRARY_H

/** @brief 按读取回调或字节缓冲区加载模块；返回持有引用的句柄，须与 MyFreeLibrary 配对。 */
HMODULE MyLoadLibrary(LPCSTR, void *, size_t, void *);

/** @brief 只查询已登记内存模块；返回借用句柄，不增加引用计数。 */
HMODULE MyGetModuleHandle(LPCSTR);

/** @brief 递减内存模块引用并在归零时卸载；系统模块交给 FreeLibrary。 */
BOOL MyFreeLibrary(HMODULE);

/** @brief 解析内存或系统模块导出，并为 GetModuleHandleExW 提供适配入口。 */
FARPROC MyGetProcAddress(HMODULE, LPCSTR);


#endif
