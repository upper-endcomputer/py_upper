/**
 * @file actctx.c
 * @brief 保存并激活 Windows DLL 的激活上下文；扩展加载后恢复原上下文，避免串扰。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#include "actctx.h"

HANDLE PyWin_DLLhActivationContext=NULL;
PFN_GETCURRENTACTCTX pfnGetCurrentActCtx=NULL;
PFN_ACTIVATEACTCTX pfnActivateActCtx=NULL;
PFN_DEACTIVATEACTCTX pfnDeactivateActCtx=NULL;
PFN_ADDREFACTCTX pfnAddRefActCtx=NULL;
PFN_RELEASEACTCTX pfnReleaseActCtx=NULL;

/** @brief 解析 Windows 激活上下文 API 地址并保存到模块全局。 */
void _MyLoadActCtxPointers()
{
	HINSTANCE hKernel32 = GetModuleHandleW(L"kernel32.dll");
	if (hKernel32)
		pfnGetCurrentActCtx = (PFN_GETCURRENTACTCTX) GetProcAddress(hKernel32, "GetCurrentActCtx");
	// If we can't load GetCurrentActCtx (ie, pre XP) , don't bother with the rest.
	if (pfnGetCurrentActCtx) {
		pfnActivateActCtx = (PFN_ACTIVATEACTCTX) GetProcAddress(hKernel32, "ActivateActCtx");
		pfnDeactivateActCtx = (PFN_DEACTIVATEACTCTX) GetProcAddress(hKernel32, "DeactivateActCtx");
		pfnAddRefActCtx = (PFN_ADDREFACTCTX) GetProcAddress(hKernel32, "AddRefActCtx");
		pfnReleaseActCtx = (PFN_RELEASEACTCTX) GetProcAddress(hKernel32, "ReleaseActCtx");
	}
}

/** @brief 激活保存的上下文并返回 cookie；失败返回零，成功后须配对停用。 */
ULONG_PTR _My_ActivateActCtx()
{
	ULONG_PTR ret = 0;
	if (PyWin_DLLhActivationContext && pfnActivateActCtx)
		if (!(*pfnActivateActCtx)(PyWin_DLLhActivationContext, &ret)) {
			OutputDebugStringA("py2exe failed to activate the activation context before loading a DLL\n");
			ret = 0; // no promise the failing function didn't change it!
		}
	return ret;
}

/** @brief 按 cookie 恢复加载前的激活上下文；零 cookie 不执行操作。 */
void _My_DeactivateActCtx(ULONG_PTR cookie)
{
	if (cookie && pfnDeactivateActCtx)
		if (!(*pfnDeactivateActCtx)(0, cookie))
			OutputDebugStringA("py2exe failed to de-activate the activation context\n");
}
