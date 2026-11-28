# 中文维护说明：提供示例业务模型；CAN、串口与 OTA 数据模型不能视为真实硬件通信实现。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""
Model Layer
纯业务逻辑层，不依赖UI框架
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseModel(ABC):
    """Model基类"""
    
    def __init__(self):
        """初始化尚未分配的模型标识。"""
        self._id: Optional[Any] = None
    
    @property
    def id(self) -> Optional[Any]:
        """返回模型当前标识；未分配时为 None。"""
        return self._id
    
    @abstractmethod
    def to_dict(self) -> dict:
        """将模型转换为字典"""
        pass
    
    @abstractmethod
    def from_dict(self, data: dict) -> None:
        """从字典加载模型"""
        pass
    
    def __repr__(self) -> str:
        """显示模型类型和标识，供日志定位对象。"""
        return f"{self.__class__.__name__}(id={self._id})"


class CANDeviceModel(BaseModel):
    """CAN设备模型"""
    
    def __init__(self):
        """初始化 CAN 配置和断开状态；不会连接硬件。"""
        super().__init__()
        self.bustype: str = "pcan"
        self.channel: str = "PCAN_USBBUS1"
        self.bitrate: int = 500000
        self.is_connected: bool = False
        self.device_type: Optional[str] = None
    
    def to_dict(self) -> dict:
        """导出 CAN 配置及连接状态为普通字典。"""
        return {
            'bustype': self.bustype,
            'channel': self.channel,
            'bitrate': self.bitrate,
            'is_connected': self.is_connected,
            'device_type': self.device_type,
        }
    
    def from_dict(self, data: dict) -> None:
        """按字典更新 CAN 字段；缺失字段恢复示例默认值。"""
        self.bustype = data.get('bustype', 'pcan')
        self.channel = data.get('channel', 'PCAN_USBBUS1')
        self.bitrate = data.get('bitrate', 500000)
        self.is_connected = data.get('is_connected', False)
        self.device_type = data.get('device_type')


class SerialDeviceModel(BaseModel):
    """串口设备模型"""
    
    def __init__(self):
        """初始化串口参数和断开状态，不打开设备。"""
        super().__init__()
        self.port: str = "COM1"
        self.baudrate: int = 19200
        self.is_connected: bool = False
        self.slave_addr: int = 1
    
    def to_dict(self) -> dict:
        """导出串口配置和状态，不包含设备句柄。"""
        return {
            'port': self.port,
            'baudrate': self.baudrate,
            'is_connected': self.is_connected,
            'slave_addr': self.slave_addr,
        }
    
    def from_dict(self, data: dict) -> None:
        """读取串口字段；缺失参数采用示例默认值。"""
        self.port = data.get('port', 'COM1')
        self.baudrate = data.get('baudrate', 19200)
        self.is_connected = data.get('is_connected', False)
        self.slave_addr = data.get('slave_addr', 1)


class OTAModel(BaseModel):
    """OTA升级模型"""
    
    def __init__(self):
        """初始化升级进度和空闲状态，不发起升级流程。"""
        super().__init__()
        self.file_path: Optional[str] = None
        self.is_upgrading: bool = False
        self.progress: float = 0.0
        self.status: str = "idle"  # idle, upgrading, completed, failed
        self.error_message: str = ""
        self.current_block: int = 0
        self.total_blocks: int = 0
    
    def to_dict(self) -> dict:
        """导出升级文件、进度、状态及错误信息。"""
        return {
            'file_path': self.file_path,
            'is_upgrading': self.is_upgrading,
            'progress': self.progress,
            'status': self.status,
            'error_message': self.error_message,
            'current_block': self.current_block,
            'total_blocks': self.total_blocks,
        }
    
    def from_dict(self, data: dict) -> None:
        """从字典恢复升级展示状态；缺失字段恢复初值。"""
        self.file_path = data.get('file_path')
        self.is_upgrading = data.get('is_upgrading', False)
        self.progress = data.get('progress', 0.0)
        self.status = data.get('status', 'idle')
        self.error_message = data.get('error_message', '')
        self.current_block = data.get('current_block', 0)
        self.total_blocks = data.get('total_blocks', 0)


class ApplicationModel(BaseModel):
    """应用程序全局模型"""
    
    def __init__(self):
        """构造子模型并初始化应用状态；应用名称由装配入口注入。"""
        super().__init__()
        self.can_device = CANDeviceModel()
        self.serial_device = SerialDeviceModel()
        self.ota = OTAModel()
        # 由装配处（compose root）从配置填入：model 持有数据，不去读文件。
        self.app_name: str = ""
        self.is_running: bool = False
        self.app_version: str = "1.0.0"
    
    def to_dict(self) -> dict:
        """递归导出应用及其设备、升级子模型的状态。"""
        return {
            'can_device': self.can_device.to_dict(),
            'serial_device': self.serial_device.to_dict(),
            'ota': self.ota.to_dict(),
            'app_name': self.app_name,
            'is_running': self.is_running,
            'app_version': self.app_version,
        }
    
    def from_dict(self, data: dict) -> None:
        """仅加载输入中存在的子模型，再更新应用级字段。"""
        if 'can_device' in data:
            self.can_device.from_dict(data['can_device'])
        if 'serial_device' in data:
            self.serial_device.from_dict(data['serial_device'])
        if 'ota' in data:
            self.ota.from_dict(data['ota'])
        self.app_name = data.get('app_name', '')
        self.is_running = data.get('is_running', False)
        self.app_version = data.get('app_version', '1.0.0')
