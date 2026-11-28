/**
 * @file actctx.h
 * @brief 声明激活上下文函数指针及共享状态；运行时检查可用性后才调用系统 API。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#include <windows.h>
#include <stdio.h>
#include <olectl.h>

#include <assert.h>
// Windows "Activation Context" work:
// Our .pyd extension modules are generally built without a manifest (ie,
// those included with Python and those built with a default distutils.
// This requires we perform some "activation context" magic when loading our
// extensions.  In summary:
// * As our DLL loads we save the context being used.
// * Before loading our extensions we re-activate our saved context.
// * After extension load is complete we restore the old context.
// As an added complication, this magic only works on XP or later - we simply
// use the existence (or not) of the relevant function pointers from kernel32.
// See bug 4566 (http://python.org/sf/4566) for more details.
#ifdef __cplusplus
extern "C" {
#endif

typedef BOOL (WINAPI * PFN_GETCURRENTACTCTX)(HANDLE *);
typedef BOOL (WINAPI * PFN_ACTIVATEACTCTX)(HANDLE, ULONG_PTR *);
typedef BOOL (WINAPI * PFN_DEACTIVATEACTCTX)(DWORD, ULONG_PTR);
typedef BOOL (WINAPI * PFN_ADDREFACTCTX)(HANDLE);
typedef BOOL (WINAPI * PFN_RELEASEACTCTX)(HANDLE);

// locals and function pointers for this activation context magic.
extern HANDLE PyWin_DLLhActivationContext;
extern PFN_GETCURRENTACTCTX pfnGetCurrentActCtx;
extern PFN_ACTIVATEACTCTX pfnActivateActCtx;
extern PFN_DEACTIVATEACTCTX pfnDeactivateActCtx;
extern PFN_ADDREFACTCTX pfnAddRefActCtx;
extern PFN_RELEASEACTCTX pfnReleaseActCtx;

/** @brief 解析 Windows 激活上下文 API 地址并保存到模块全局。 */
void _MyLoadActCtxPointers();
/** @brief 激活保存的上下文并返回 cookie；失败返回零，成功后须配对停用。 */
ULONG_PTR _My_ActivateActCtx();
/** @brief 按 cookie 恢复加载前的激活上下文；零 cookie 不执行操作。 */
void _My_DeactivateActCtx(ULONG_PTR cookie);

#ifdef __cplusplus
}
#endif
