# 中文维护说明：管理应用拥有的异步任务与关闭回调；并发关闭共享任务，等待者取消不应中断清理。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Application-owned asyncio tasks and service shutdown, independent of Qt."""
from __future__ import annotations
import asyncio
import inspect


class AsyncLifecycle:
    def __init__(self):
        """创建本应用独占的任务集合、关闭回调和幂等关闭状态。"""
        self._tasks = set()
        self._closers = []
        self._closing = False
        self._closed = False
        self._close_task = None

    def create_task(self, coroutine):
        """在当前循环创建受管任务；关闭期间拒绝新任务并关闭未调度协程。"""
        if self._closing:
            coroutine.close()
            raise RuntimeError('Application is shutting down')
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        """移除完成任务；未取消任务的异常交给事件循环异常处理器。"""
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            task.get_loop().call_exception_handler({'message': 'Application task failed', 'exception': task.exception(), 'task': task})

    def register_close(self, callback):
        """登记关闭回调，退出时按逆注册顺序调用；开始关闭后拒绝注册。"""
        if self._closing:
            raise RuntimeError('Application is shutting down')
        self._closers.append(callback)

    # 只创建一次关闭任务；shield 将调用方取消与资源清理任务分离。
    async def aclose(self):
        """共享唯一清理任务；等待者取消不传播给清理，已完成关闭可重复调用。"""
        if self._closed:
            return
        if self._close_task is None:
            self._closing = True
            caller = asyncio.current_task()
            self._close_task = asyncio.create_task(self._close(caller))
        await asyncio.shield(self._close_task)

    async def _close(self, caller):
        """取消受管任务并等待退出，再逆序关闭服务；单个失败不跳过其余清理。"""
        tasks = tuple(task for task in self._tasks if task is not caller)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        errors = []
        for close in reversed(self._closers):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
        self._closers.clear()
        self._closed = True
        if errors:
            raise RuntimeError('Service shutdown failed') from errors[0]
