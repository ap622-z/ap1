"""后端配置包：统一从 config.settings 再导出。"""
from .settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
