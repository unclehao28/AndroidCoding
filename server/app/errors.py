"""统一的 API 错误类型。

约定：
- 请求本身的错误（路径非法、根目录外、不存在、不是文件）返回 4xx；
- 领域状态（二进制、超出大小限制、编码不可解码）返回 200 + status 字段，
  由前端显式展示，不伪装成成功也不伪装成崩溃。
"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict:
        payload = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


def invalid_path(message: str, **details) -> ApiError:
    return ApiError(400, "invalid_path", message, details)


def outside_root(message: str, **details) -> ApiError:
    return ApiError(403, "path_outside_root", message, details)


def not_found(message: str, **details) -> ApiError:
    return ApiError(404, "not_found", message, details)


def not_a_file(message: str, **details) -> ApiError:
    return ApiError(409, "not_a_file", message, details)


def invalid_request(message: str, **details) -> ApiError:
    return ApiError(400, "invalid_request", message, details)


def unknown_workspace(workspace_id: str) -> ApiError:
    return ApiError(404, "unknown_workspace", f"未配置的工作区：{workspace_id}", {"workspaceId": workspace_id})
