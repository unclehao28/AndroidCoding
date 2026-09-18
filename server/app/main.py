"""安卓源码工作台后端（P1）。

只做真实文件能力：健康状态、工作区、分层目录、文件/行段读取、受限全文检索、取消检索。
不提供写入（P4）、不提供语义跳转（P2）——这些接口会显式返回未就绪状态。
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .config import API_VERSION, VERSION, AppConfig, RootConfig
from .errors import (
    ApiError,
    invalid_request,
    not_a_file,
    not_found,
    unknown_workspace,
)
from .gitremote import SyncManager, git_binary, git_version, read_state as read_git_state
from .lsp.manager import LanguageServerManager
from .navigation import NavigationService
from .pathtools import (
    decode_source,
    detect_eol,
    detect_language,
    ensure_readable_file,
    is_within,
    resolve_under_root,
)
from .search import SearchManager, SearchRequest, validate_query

def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")


# 注意：pydantic 与 FastAPI 会在运行时求值注解（typing.get_type_hints），
# 因此对外暴露的模型与端点必须使用 typing.Optional/Dict 这类写法，
# 不能用 `str | None` / `dict[str, Any]`——Python 3.8（Ubuntu 20.04 自带）
# 无法求值这两种语法，而公司服务器上很可能只有 3.8。
class SearchBody(CamelModel):
    query: str
    workspace: str
    scope: str = ""
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False
    include_glob: Optional[str] = None
    limit: Optional[int] = None
    request_id: Optional[str] = None


class CancelBody(CamelModel):
    request_id: str


class NavigationBody(CamelModel):
    workspace: str
    path: str
    kind: str = "definition"
    source_version: Optional[str] = None
    position: Optional[Dict[str, Any]] = None


class SyncBody(CamelModel):
    workspace: str


# 远程工作区"是否可用"需要问 git（目录存在不代表 clone 完整：中断的 clone 只留下 .git）。
# 结果缓存几秒，避免每次读文件都起一串 git 子进程。
_CHECKOUT_VALIDITY: Dict[str, tuple[float, bool]] = {}
CHECKOUT_CACHE_SECONDS = 5.0


def _remote_checkout_ready(root: RootConfig) -> bool:
    import time

    key = str(root.path)
    now = time.time()
    cached = _CHECKOUT_VALIDITY.get(key)
    if cached and now - cached[0] < CHECKOUT_CACHE_SECONDS:
        return cached[1]
    state = read_git_state(root.path)
    ready = bool(state.get("isRepo")) and state.get("head") is not None
    _CHECKOUT_VALIDITY[key] = (now, ready)
    return ready


def _require_root(config: AppConfig, workspace: str) -> RootConfig:
    root = config.root(workspace)
    if root is None:
        raise unknown_workspace(workspace)
    if root.is_remote:
        if _remote_checkout_ready(root):
            return root
        raise ApiError(
            409,
            "workspace_not_synced",
            f"远程工作区 {root.id} 尚未同步到本地缓存：{root.path}",
            {
                "workspaceId": root.id,
                "url": root.git.url if root.git else None,
                "checkout": str(root.path),
                "hint": "在服务器执行 python3 -m app --sync %s，或点界面上的「同步」按钮" % root.id,
            },
        )
    if not root.path.is_dir():
        raise not_found("工作区根目录当前不可用", workspace=workspace, path=str(root.path))
    return root


def _workspace_payload(root: RootConfig) -> Dict[str, Any]:
    payload = root.to_public()
    if root.is_remote:
        state = read_git_state(
            root.path,
            url=root.git.url if root.git else None,
            ref=root.git.ref if root.git else None,
        )
        payload["sync"] = state
        # 远程工作区"可用"的判定不能只看目录是否存在：被中断的 clone 会留下只有 .git、
        # 没有 HEAD 的半成品，这种情况必须当成未同步，让界面提示重新同步。
        payload["exists"] = bool(state.get("isRepo")) and state.get("head") is not None
        payload["partial"] = bool(state.get("isRepo")) and state.get("head") is None
    return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_app(
    config: AppConfig,
    search_manager: Optional[SearchManager] = None,
    sync_manager: Optional[SyncManager] = None,
    navigation_service: Optional[NavigationService] = None,
) -> FastAPI:
    manager = search_manager or SearchManager(config)
    syncs = sync_manager or SyncManager(config)
    language_servers = getattr(navigation_service, "manager", None) or LanguageServerManager(
        config.navigation, config.roots
    )
    navigation = navigation_service or NavigationService(config, language_servers)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # 停机时必须关掉语言服务子进程，否则会在构建服务器上留下孤儿进程
        await language_servers.shutdown_all()

    app = FastAPI(
        lifespan=lifespan,
        title="安卓源码工作台后端",
        version=VERSION,
        description="P1：真实目录读取与受限检索。写入与语义导航尚未实现。",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.server.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = config
    app.state.search_manager = manager
    app.state.sync_manager = syncs
    app.state.language_servers = language_servers
    app.state.navigation = navigation

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "mode": "real",
            "version": VERSION,
            "apiVersion": API_VERSION,
            "time": _now_iso(),
            "serverTimeMs": int(time.time() * 1000),
            "search": manager.engine_info(),
            "activeSearches": manager.active_request_ids,
            "features": {"write": config.features.write, "navigation": config.features.navigation},
            "git": {
                "available": git_binary() is not None,
                "version": git_version(),
                "remoteRoots": [root.id for root in config.remote_roots],
                "cacheDir": str(config.cache_dir) if config.cache_dir else None,
                "note": "远程仓库为只读：只做 clone/fetch/merge --ff-only，不 reset --hard，不自动提交或推送",
            },
            "sync": syncs.snapshot(),
            "navigation": navigation.status(),
            "capabilities": {
                "write": False,
                "navigation": False,
                "index": False,
                "note": "write 属于 P4，navigation/index 属于 P2/P3；当前均为未就绪",
            },
            "warnings": list(config.warnings),
        }

    @app.get("/api/workspaces")
    async def workspaces() -> Dict[str, Any]:
        items = [_workspace_payload(root) for root in config.roots]
        return {
            "workspaces": items,
            "count": len(items),
            "cacheDir": str(config.cache_dir) if config.cache_dir else None,
            "sync": syncs.snapshot(),
            "navigation": navigation.status(),
        }

    @app.post("/api/workspace/sync")
    async def start_sync(body: SyncBody) -> Dict[str, Any]:
        root = config.root(body.workspace)
        if root is None:
            raise unknown_workspace(body.workspace)
        if not root.is_remote:
            raise invalid_request(
                f"工作区 {root.id} 是本地目录，不需要同步",
                workspaceId=root.id,
                hint="只有配置了 git.url 的远程仓库才需要同步",
            )
        if git_binary() is None:
            raise ApiError(503, "git_unavailable", "服务器上没有 git，无法同步远程仓库")
        started, message = syncs.start(root)
        return {"started": started, "workspaceId": root.id, "message": message, "sync": syncs.snapshot()}

    @app.get("/api/workspace/sync")
    async def sync_status() -> Dict[str, Any]:
        snapshot = syncs.snapshot()
        if not snapshot["running"]:
            # 同步结束后状态可能已经变化（例如刚 clone 出有效 HEAD），清掉缓存让它重新判断
            _CHECKOUT_VALIDITY.clear()
        return snapshot

    @app.post("/api/workspace/sync/cancel")
    async def cancel_sync() -> Dict[str, Any]:
        cancelled = syncs.cancel()
        return {"cancelled": cancelled, "sync": syncs.snapshot()}

    @app.get("/api/config")
    async def public_config() -> Dict[str, Any]:
        return config.to_public()

    @app.get("/api/tree")
    async def tree(workspace: str = Query(...), path: str = Query("")) -> Dict[str, Any]:
        root = _require_root(config, workspace)
        target, rel = resolve_under_root(root.path, path)
        if not target.is_dir():
            raise not_a_file("目标是文件，不是目录", path=rel)
        limit = config.limits.max_tree_entries
        entries: List[Dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(target) as iterator:
                raw_entries = sorted(iterator, key=lambda item: (not item.is_dir(), item.name.lower()))
        except PermissionError:
            raise ApiError(403, "permission_denied", f"服务器无权读取目录：{rel}", {"path": rel}) from None
        for entry in raw_entries:
            if len(entries) >= limit:
                truncated = True
                break
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            is_symlink = entry.is_symlink()
            escaping = False
            if is_symlink:
                try:
                    escaping = not is_within(Path(entry.path).resolve(), root.path)
                except OSError:
                    escaping = True
            if entry.is_dir(follow_symlinks=not escaping):
                kind = "dir"
            elif entry.is_file(follow_symlinks=not escaping):
                kind = "file"
            else:
                kind = "other"
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            entries.append(
                {
                    "name": entry.name,
                    "path": child_rel,
                    "type": kind,
                    "size": None if kind == "dir" else stat.st_size,
                    "symlink": is_symlink,
                    "escaping": escaping,
                    "accessible": not escaping,
                }
            )
        return {
            "workspaceId": root.id,
            "workspaceName": root.name,
            "path": rel,
            "parent": rel.rsplit("/", 1)[0] if "/" in rel else ("" if rel else None),
            "entries": entries,
            "entryCount": len(entries),
            "truncated": truncated,
            "limit": limit,
            "readonly": root.readonly,
        }

    @app.get("/api/file")
    async def read_file(
        workspace: str = Query(...),
        path: str = Query(...),
        startLine: int = Query(0, ge=0),
        lineCount: Optional[int] = Query(None, ge=1),
    ) -> Dict[str, Any]:
        root = _require_root(config, workspace)
        target, rel = resolve_under_root(root.path, path)
        stat = ensure_readable_file(target, requested=rel)
        base: Dict[str, Any] = {
            "workspaceId": root.id,
            "path": rel,
            "name": target.name,
            "language": detect_language(target.name),
            "size": stat.st_size,
            "mtimeMs": int(stat.st_mtime * 1000),
            "readonly": root.readonly,
            "symlink": target.is_symlink(),
        }
        max_bytes = config.limits.max_file_bytes
        if stat.st_size > max_bytes:
            return {
                **base,
                "status": "too_large",
                "message": f"文件 {stat.st_size} 字节，超过 maxFileBytes={max_bytes}；请调整配置或使用命令行工具处理",
                "lines": [],
                "startLine": 0,
                "lineCount": 0,
                "totalLines": None,
                "hasMore": False,
                "hash": None,
            }
        with open(target, "rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return {
                **base,
                "status": "too_large",
                "message": f"文件在读取过程中超过 maxFileBytes={max_bytes}",
                "lines": [],
                "startLine": 0,
                "lineCount": 0,
                "totalLines": None,
                "hasMore": False,
                "hash": None,
            }
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        decoded = decode_source(raw)
        if not decoded.ok:
            return {
                **base,
                "status": decoded.status,
                "message": decoded.message,
                "lines": [],
                "startLine": 0,
                "lineCount": 0,
                "totalLines": None,
                "hasMore": False,
                "hash": digest,
                "encoding": decoded.encoding,
            }
        total = len(decoded.lines)
        max_lines = config.limits.max_line_count
        requested_lines = lineCount if lineCount is not None else max_lines
        clamped = requested_lines > max_lines
        effective = min(requested_lines, max_lines)
        if startLine >= total:
            effective = 0
        window = decoded.lines[startLine : startLine + effective] if effective else ()
        end = startLine + len(window)
        return {
            **base,
            "status": "ok",
            "encoding": decoded.encoding,
            "lossy": decoded.lossy,
            "eol": detect_eol(raw),
            "hash": digest,
            "lines": list(window),
            "startLine": startLine,
            "lineCount": len(window),
            "totalLines": total,
            "hasMore": end < total,
            "lineCountClamped": clamped,
            "maxLineCount": max_lines,
        }

    @app.post("/api/search")
    async def search(body: SearchBody) -> Dict[str, Any]:
        root = _require_root(config, body.workspace)
        literal = not body.regex
        query = validate_query(body.query, literal=literal, max_length=config.limits.max_regex_length)
        if body.include_glob is not None and body.include_glob.strip().startswith("!"):
            raise invalid_request("includeGlob 不能以 ! 开头，排除规则请放在配置的 excludeGlobs 中")
        start_dir, subpath = resolve_under_root(root.path, body.scope or "")
        if not start_dir.is_dir():
            raise not_a_file("scope 必须是目录", path=subpath)
        limit = min(body.limit or config.limits.max_search_results, config.limits.max_search_results)
        request_id = body.request_id or f"req-{uuid.uuid4().hex[:16]}"
        request = SearchRequest(
            request_id=request_id,
            root=root,
            query=query,
            start_dir=start_dir,
            subpath=subpath,
            literal=literal,
            case_sensitive=body.case_sensitive,
            whole_word=body.whole_word,
            include_glob=body.include_glob or None,
            limit=limit,
            timeout_seconds=config.limits.search_timeout_seconds,
            max_file_bytes=config.limits.max_search_file_bytes,
            max_line_length=config.limits.max_line_length,
            exclude_globs=config.search.exclude_globs,
            respect_ignore_files=config.search.respect_ignore_files,
            python_max_files=config.search.python_max_files,
            rg_threads=config.limits.rg_threads,
        )
        started = time.perf_counter()
        outcome = await manager.run(request)
        payload = outcome.to_payload()
        payload.update(
            {
                "requestId": request_id,
                "query": query,
                "workspaceId": root.id,
                "scope": subpath,
                "limit": limit,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "options": {
                    "regex": body.regex,
                    "caseSensitive": body.case_sensitive,
                    "wholeWord": body.whole_word,
                    "includeGlob": body.include_glob or None,
                },
                "columnUnit": "unicode-code-points",
                "lineBase": 0,
                "indexed": False,
                "indexNote": "P1 未建立全库索引，检索为受限制的目录扫描（全库索引属于 P3）",
            }
        )
        return payload

    @app.post("/api/search/cancel")
    async def cancel_search(body: CancelBody) -> Dict[str, Any]:
        cancelled = manager.cancel(body.request_id)
        return {
            "requestId": body.request_id,
            "cancelled": cancelled,
            "activeSearches": manager.active_request_ids,
            "note": "" if cancelled else "该 requestId 没有正在运行的检索（可能已结束）",
        }

    @app.post("/api/navigation")
    async def navigation_lookup(body: NavigationBody) -> Dict[str, Any]:
        root = _require_root(config, body.workspace)
        resolved, rel = resolve_under_root(root.path, body.path)
        if not resolved.exists():
            raise not_found("文件不存在", path=rel)
        if not resolved.is_file():
            raise not_a_file("目标是目录，不是文件", path=rel)
        if body.position is not None and not isinstance(body.position, dict):
            raise invalid_request("position 必须是对象：{line, character}（0 基行号 + UTF-16 列）")
        payload = await navigation.navigate(
            root=root,
            rel_path=rel,
            resolved_path=resolved,
            position=body.position or {"line": 0, "character": 0},
            kind=body.kind,
            source_version=body.source_version,
        )
        payload["fallback"] = {
            "available": True,
            "kind": "text",
            "endpoint": "/api/search",
            "note": "字面量检索结果只表示文本匹配，不代表定义或引用；注释与字符串也会命中",
        }
        payload["capability"] = {
            "navigation": navigation.enabled,
            "server": (payload.get("server") or {}).get("name"),
            "phase": "P2（C/C++ 已接入 clangd；Java/Kotlin/Rust/AIDL 未接入）",
        }
        return payload

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def api_unknown(rest: str) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "unknown_route", "message": f"未知接口：/api/{rest}"}},
        )

    app.mount("/", StaticFiles(directory=str(config.prototype_dir), html=True), name="prototype")
    return app


def create_app_from_env() -> FastAPI:
    """供 `uvicorn --factory app.main:create_app_from_env` 使用（配置来自 ASW_CONFIG 或默认位置）。"""
    from .config import load_config, resolve_config_path

    return create_app(load_config(resolve_config_path()))
