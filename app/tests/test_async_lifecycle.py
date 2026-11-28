# 中文维护说明：验证异步任务取消与资源关闭；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import asyncio
import pytest
from mvvm.async_lifecycle import AsyncLifecycle
from mvvm.core.command import AsyncRelayCommand


def test_tasks_cancel_before_services_close_and_reject_new_work():
    """验证关闭时先取消任务，再逆序关闭服务，并拒绝新增任务。"""
    async def scenario():
        """在单个事件循环中验证关闭顺序与重复关闭的幂等性。"""
        owner = AsyncLifecycle()
        events = []
        async def work():
            """保持任务运行至取消，并记录 finally 清理事件。"""
            try:
                await asyncio.Event().wait()
            finally:
                events.append('cancelled')
        async def close():
            """模拟让出事件循环后才能完成的异步服务关闭。"""
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
    """验证单个服务关闭异常不会阻止其余服务清理。"""
    async def scenario():
        """收集关闭事件并确认汇总异常保留失败信息。"""
        owner = AsyncLifecycle()
        events = []
        owner.register_close(lambda: events.append('closed'))
        def fail():
            """注入服务关闭异常，覆盖异常后继续清理的路径。"""
            raise ValueError('device failed')
        owner.register_close(fail)
        with pytest.raises(RuntimeError, match='Service shutdown'):
            await owner.aclose()
        assert events == ['closed']
    asyncio.run(scenario())


def test_owned_command_prevents_duplicate_scheduling_and_is_cancelled():
    """验证受生命周期管理的命令不会重复启动且能随容器取消。"""
    async def scenario():
        """连续触发同一命令，再关闭所有者检查执行状态恢复。"""
        owner = AsyncLifecycle()
        calls = []
        async def work(_):
            """记录命令启动并等待取消，制造重复调度检查窗口。"""
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
