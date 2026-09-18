"""安卓源码工作台后端（P1：真实目录读取 + 受限检索）。

模块划分：
- config：配置加载与校验；
- pathtools：路径边界、编码、二进制判定；
- search：检索引擎（ripgrep 优先，Python 受限兜底）与取消管理；
- main：FastAPI 应用与接口。
"""
from .config import API_VERSION, VERSION

__all__ = ["API_VERSION", "VERSION"]
