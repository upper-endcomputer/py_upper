# 中文维护说明：验证异步任务取消与资源关闭；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import asyncio
import pytest
from mvvm.async_lifecycle import AsyncLifecycle
from mvvm.core.command import AsyncRelayCommand


def test_tasks_cancel_before_services_close_and_reject_new_work():
    async def scenario():
        owner = AsyncLifecycle()
        events = []
        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                events.append('cancelled')
        async def close():
            await asyncio.sleep(0)
            events.append('service')
        owner.register_close(lambda: events.append('first'))
        owner.register_close(close)
        task = owner.create_task(work())
        await asyncio.sleep(0)
        await owner.aclose()
        await owner.aclose()
        assert task.cancelled()
        assert events == ['cancelled', 'service', 'first']
        with pytest.raises(RuntimeError):
            owner.create_task(work())
    asyncio.run(scenario())


def test_service_failure_does_not_skip_other_services():
    async def scenario():
        owner = AsyncLifecycle()
        events = []
        owner.register_close(lambda: events.append('closed'))
        def fail():
            raise ValueError('device failed')
        owner.register_close(fail)
        with pytest.raises(RuntimeError, match='Service shutdown'):
            await owner.aclose()
        assert events == ['closed']
    asyncio.run(scenario())


def test_owned_command_prevents_duplicate_scheduling_and_is_cancelled():
    async def scenario():
        owner = AsyncLifecycle()
        calls = []
        async def work(_):
            calls.append('started')
            await asyncio.Event().wait()
        command = AsyncRelayCommand(work, task_owner=owner)
        command.execute()
        command.execute()
        await asyncio.sleep(0)
        assert calls == ['started']
        assert command.is_executing
        await owner.aclose()
        assert not command.is_executing
    asyncio.run(scenario())
