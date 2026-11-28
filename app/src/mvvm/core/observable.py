# 中文维护说明：维护可观察属性及订阅回调；解除订阅是视图销毁时的必要步骤。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""
Observable property system for MVVM
支持属性变化通知和数据绑定
"""

from typing import Any, Callable, Optional, List, TypeVar, Generic


T = TypeVar('T')


class PropertyChangedEvent:
    """属性变化事件"""
    
    def __init__(self, property_name: str, old_value: Any, new_value: Any):
        """记录属性名、变更前值和变更后值，供订阅者读取。"""
        self.property_name = property_name
        self.old_value = old_value
        self.new_value = new_value
    
    def __repr__(self) -> str:
        """显示属性名和前后值，便于诊断通知顺序。"""
        return f"PropertyChangedEvent({self.property_name}: {self.old_value} -> {self.new_value})"


class ObservableProperty(Generic[T]):
    """可观察属性 - 支持数据绑定和变化通知"""
    
    def __init__(self, default_value: Optional[T] = None):
        """保存初值并初始化独立订阅列表；属性名称稍后由注册方设置。"""
        self._value: Optional[T] = default_value
        self._listeners: List[Callable[[PropertyChangedEvent], None]] = []
        self._property_name: Optional[str] = None
    
    def set_property_name(self, name: str) -> None:
        """设置属性名称（由ViewModel自动调用）"""
        self._property_name = name
    
    def get_value(self) -> Optional[T]:
        """获取属性值"""
        return self._value
    
    def set_value(self, value: Optional[T]) -> bool:
        """设置属性值，返回是否发生了变化"""
        if self._value != value:
            old_value = self._value
            self._value = value
            self._notify_changed(old_value, value)
            return True
        return False
    
    def _notify_changed(self, old_value: Any, new_value: Any) -> None:
        """通知所有监听器属性已变化"""
        event = PropertyChangedEvent(
            property_name=self._property_name or "unknown",
            old_value=old_value,
            new_value=new_value
        )
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception as e:
                print(f"Error in property changed listener: {e}")
    
    def subscribe(self, callback: Callable[[PropertyChangedEvent], None]) -> Callable[[], None]:
        """
        订阅属性变化事件
        返回一个unsubscribe函数
        """
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None
    
    def __get__(self, obj: Any, objtype: Optional[type] = None) -> Optional[T]:
        """类级访问返回描述符本身，实例访问返回描述符保存的当前值。

        Descriptor get"""
        if obj is None:
            return self
        return self._value
    
    def __set__(self, obj: Any, value: Optional[T]) -> None:
        """把赋值交给 set_value，复用相等判断与变化通知。

        Descriptor set"""
        self.set_value(value)

