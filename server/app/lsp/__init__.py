"""语言服务（LSP）接入层。

- `protocol.py`：stdio 上的 Content-Length 分帧与 JSON-RPC 编解码；
- `positions.py`：UTF-16 行列 ↔ Python 字符偏移的转换（含非 BMP 字符）；
- `client.py`：单个语言服务进程的异步客户端（请求、通知、超时、关闭）；
- `manager.py`：按工作区/语言管理语言服务进程（懒启动、复用、上限、空闲关闭）。

定位说明：这一层只把"真实语言服务返回的位置"如实交给上层，
不做任何"猜一个同名符号"的兜底——那是候选（symbol/text）层的事。
"""
from .client import LspClient, LspError, LspProcessError, LspTimeoutError
from .positions import (
    codepoint_column_to_utf16,
    codepoint_to_utf16,
    column_to_codepoint,
    make_range,
    range_to_text,
    split_lines,
    utf16_index_to_codepoint,
    utf16_len,
    word_at,
)
from .protocol import LspProtocolError, MessageBuffer, encode_message

__all__ = [
    "LspClient",
    "LspError",
    "LspProcessError",
    "LspTimeoutError",
    "LspProtocolError",
    "MessageBuffer",
    "encode_message",
    "codepoint_to_utf16",
    "codepoint_column_to_utf16",
    "column_to_codepoint",
    "utf16_index_to_codepoint",
    "utf16_len",
    "split_lines",
    "word_at",
    "make_range",
    "range_to_text",
]
