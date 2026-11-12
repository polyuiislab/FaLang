"""
Function Block 配置模块。

与代码生成模块并行独立，位于 Post-RA 调度之后。
提供 FB 网络构建与导出（JSON / IEC 61499 XML）。
"""
from compiler.fb_config.fb_model import FBConfig, FBNetwork, FBInstance, Connection
from compiler.fb_config.fb_builder import FBConfigBuilder
from compiler.fb_config.fb_exporter import FBExporter

__all__ = [
    "FBConfig",
    "FBNetwork",
    "FBInstance",
    "Connection",
    "FBConfigBuilder",
    "FBExporter",
]
