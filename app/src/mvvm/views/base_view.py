"""
View Layer - Pure UI Components with MVVM Data Binding
"""

from typing import Any, Optional, Callable, List
from mvvm.core import BaseViewModel, PropertyChangedEvent


class MVVMBinding:
    """MVVM数据绑定 - 将UI控件与ViewModel属性绑定"""
    
    def __init__(self, viewmodel: BaseViewModel, property_name: str):
        self.viewmodel = viewmodel
        self.property_name = property_name
        self._ui_elements: List[Any] = []
        self._value_converters: List[Callable[[Any], Any]] = []
        
        # 订阅ViewModel属性变化
        property_obj = viewmodel.get_property(property_name)
        if property_obj:
            property_obj.subscribe(self._on_viewmodel_property_changed)
    
    def add_ui_element(
        self,
        ui_element: Any,
        attribute_name: str = 'value',
        getter_name: Optional[str] = None,
    ) -> None:
        """添加UI元素到绑定

        attribute_name 要么是可写属性名（setattr），要么是 setter 方法名
        （QLabel.setText 这类，属性变化时被调用）。

        反向（UI → ViewModel）必须显式给出 getter_name：把 setter 名字反推成
        getter 是实现细节（Qt 里 setText/text、setWindowTitle/windowTitle 并不
        同形），猜错了就是静默读错值。
        """
        self._ui_elements.append({
            'element': ui_element,
            'attribute': attribute_name,
            'getter': getter_name,
        })
    
    def add_value_converter(self, converter: Callable[[Any], Any]) -> None:
        """添加值转换器"""
        self._value_converters.append(converter)
    
    def _on_viewmodel_property_changed(self, event: PropertyChangedEvent) -> None:
        """ViewModel属性变化时更新UI"""
        value = event.new_value
        
        # 应用值转换器
        for converter in self._value_converters:
            value = converter(value)
        
        # 更新所有关联的UI元素。setter 方法直接调用，普通属性才 setattr：
        # setattr(label, 'setText', value) 只会把方法名覆盖成一个属性，界面不动。
        for ui_info in self._ui_elements:
            target = getattr(ui_info['element'], ui_info['attribute'])
            if callable(target):
                target(value)
            else:
                setattr(ui_info['element'], ui_info['attribute'], value)
    
    def update_viewmodel_from_ui(self) -> None:
        """从UI把第一个元素的值写回ViewModel"""
        if not self._ui_elements:
            return

        ui_info = self._ui_elements[0]
        getter_name = ui_info['getter']
        if getter_name is None:
            raise ValueError(
                f"binding {self.property_name!r} has no getter: "
                f"pass getter_name to add_ui_element() for two-way binding"
            )
        getter = getattr(ui_info['element'], getter_name)
        value = getter() if callable(getter) else getter
        self.viewmodel.set_property_value(self.property_name, value)


class CommandBinding:
    """命令绑定 - 将UI事件与ViewModel命令绑定"""
    
    def __init__(self, viewmodel: BaseViewModel, command_name: str):
        self.viewmodel = viewmodel
        self.command_name = command_name
        self.command = getattr(viewmodel, f"{command_name}_command", None)
        self._ui_elements: List[Any] = []
    
    def add_ui_element(self, ui_element: Any, event_name: str = 'clicked') -> None:
        """添加UI元素到绑定"""
        self._ui_elements.append({
            'element': ui_element,
            'event': event_name,
        })
        
        # 连接UI事件到命令
        if self.command:
            try:
                event_obj = getattr(ui_element, event_name)
                event_obj.connect(lambda param=None: self.command.execute(param))
            except Exception as e:
                print(f"Error binding command: {e}")
    
    def execute(self, parameter: Any = None) -> None:
        """执行命令"""
        if self.command:
            self.command.execute(parameter)


class ViewBase:
    """View基类 - 所有View应继承此类"""
    
    def __init__(self, viewmodel: Optional[BaseViewModel] = None):
        self.viewmodel = viewmodel
        self._bindings: List[MVVMBinding] = []
        self._command_bindings: List[CommandBinding] = []
    
    def bind_property(
        self,
        property_name: str,
        ui_element: Any,
        attribute_name: str = 'value',
        getter_name: Optional[str] = None,
    ) -> MVVMBinding:
        """绑定ViewModel属性到UI元素（attribute_name 可为 setter 方法名）"""
        if not self.viewmodel:
            raise ValueError("ViewModel not set")
        
        binding = MVVMBinding(self.viewmodel, property_name)
        binding.add_ui_element(ui_element, attribute_name, getter_name)
        self._bindings.append(binding)
        return binding
    
    def bind_command(self, command_name: str, ui_element: Any, event_name: str = 'clicked') -> CommandBinding:
        """绑定ViewModel命令到UI事件"""
        if not self.viewmodel:
            raise ValueError("ViewModel not set")
        
        binding = CommandBinding(self.viewmodel, command_name)
        binding.add_ui_element(ui_element, event_name)
        self._command_bindings.append(binding)
        return binding
    
    def show(self) -> None:
        """显示View"""
        pass
    
    def close(self) -> None:
        """关闭View"""
        pass
    
    def dispose(self) -> None:
        """清理资源"""
        self._bindings.clear()
        self._command_bindings.clear()
        if self.viewmodel:
            self.viewmodel.dispose()
