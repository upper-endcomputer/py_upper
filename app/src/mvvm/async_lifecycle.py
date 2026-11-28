"""Application-owned asyncio tasks and service shutdown, independent of Qt."""
from __future__ import annotations
import asyncio
import inspect


class AsyncLifecycle:
    def __init__(self):
        self._tasks = set()
        self._closers = []
        self._closing = False
        self._closed = False
        self._close_task = None

    def create_task(self, coroutine):
        if self._closing:
            coroutine.close()
            raise RuntimeError('Application is shutting down')
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            task.get_loop().call_exception_handler({'message': 'Application task failed', 'exception': task.exception(), 'task': task})

    def register_close(self, callback):
        if self._closing:
            raise RuntimeError('Application is shutting down')
        self._closers.append(callback)

    async def aclose(self):
        if self._closed:
            return
        if self._close_task is None:
            self._closing = True
            caller = asyncio.current_task()
            self._close_task = asyncio.create_task(self._close(caller))
        await asyncio.shield(self._close_task)

    async def _close(self, caller):
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
