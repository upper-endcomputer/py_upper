"""
Command system for MVVM
支持命令绑定和异步命令执行
"""

from typing import Callable, Any, Optional, Coroutine
import asyncio


class RelayCommand:
    """中继命令 - 将命令操作委托给外部方法"""
    
    def __init__(
        self,
        execute: Callable[..., None],
        can_execute: Optional[Callable[..., bool]] = None
    ):
        """
        Args:
            execute: 执行命令的方法
            can_execute: 判断是否可以执行的方法（可选）
        """
        self._execute = execute
        self._can_execute = can_execute or (lambda *args, **kwargs: True)
        self._can_execute_changed_listeners = []
    
    def can_execute(self, parameter: Any = None) -> bool:
        """判断命令是否可以执行"""
        try:
            try:
                return self._can_execute(parameter)
            except TypeError:
                return self._can_execute()
        except Exception as e:
            print(f"Error checking command execution: {e}")
            return False
    
    def execute(self, parameter: Any = None) -> None:
        """执行命令"""
        try:
            if self.can_execute(parameter):
                self._execute(parameter)
        except Exception as e:
            print(f"Error executing command: {e}")
    
    def subscribe_can_execute_changed(self, callback: Callable) -> Callable[[], None]:
        """
        订阅命令执行状态变化
        返回unsubscribe函数
        """
        self._can_execute_changed_listeners.append(callback)
        return lambda: self._can_execute_changed_listeners.remove(callback) if callback in self._can_execute_changed_listeners else None
    
    def notify_can_execute_changed(self) -> None:
        """通知命令执行状态已变化"""
        for listener in self._can_execute_changed_listeners:
            try:
                listener()
            except Exception as e:
                print(f"Error in can_execute_changed listener: {e}")


class AsyncRelayCommand:
    """异步中继命令 - 支持异步执行"""
    
    def __init__(
        self,
        execute: Callable[..., Coroutine],
        can_execute: Optional[Callable[..., bool]] = None
    ):
        """
        Args:
            execute: 异步执行方法
            can_execute: 判断是否可以执行的方法
        """
        self._execute = execute
        self._can_execute = can_execute or (lambda *args, **kwargs: True)
        self._is_executing = False
        self._can_execute_changed_listeners = []
    
    @property
    def is_executing(self) -> bool:
        """判断是否正在执行"""
        return self._is_executing
    
    def can_execute(self, parameter: Any = None) -> bool:
        """判断命令是否可以执行"""
        if self._is_executing:
            return False
        try:
            try:
                return self._can_execute(parameter)
            except TypeError:
                return self._can_execute()
        except Exception as e:
            print(f"Error checking command execution: {e}")
            return False
    
    async def execute_async(self, parameter: Any = None) -> None:
        """异步执行命令"""
        if not self.can_execute(parameter):
            return
        
        try:
            self._is_executing = True
            self.notify_can_execute_changed()
            await self._execute(parameter)
        except Exception as e:
            print(f"Error executing async command: {e}")
        finally:
            self._is_executing = False
            self.notify_can_execute_changed()
    
    def execute(self, parameter: Any = None) -> None:
        """同步执行异步命令（需要在事件循环中运行）"""
        if not self.can_execute(parameter):
            return
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果事件循环正在运行，需要使用 create_task
                asyncio.ensure_future(self.execute_async(parameter))
            else:
                # 否则直接运行
                loop.run_until_complete(self.execute_async(parameter))
        except Exception as e:
            print(f"Error executing async command: {e}")
    
    def subscribe_can_execute_changed(self, callback: Callable) -> Callable[[], None]:
        """订阅命令状态变化"""
        self._can_execute_changed_listeners.append(callback)
        return lambda: self._can_execute_changed_listeners.remove(callback) if callback in self._can_execute_changed_listeners else None
    
    def notify_can_execute_changed(self) -> None:
        """通知命令状态已变化"""
        for listener in self._can_execute_changed_listeners:
            try:
                listener()
            except Exception as e:
                print(f"Error in can_execute_changed listener: {e}")
