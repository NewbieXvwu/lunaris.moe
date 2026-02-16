"""本地离线服务器 — 从 site/ 和 api/ 提供静态文件。

支持两种引擎：
- stdlib 模式：基于 http.server，零额外依赖
- ASGI 模式：基于 starlette + hypercorn，高并发异步 IO

启动时自动检测：安装了 starlette 和 hypercorn 则使用 ASGI 模式，否则回退 stdlib。
"""

from __future__ import annotations

import gzip
import http.server
import io
import json
import logging
import os
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .common import API_ORIGIN, LUNARIS_ORIGIN

API_PREFIX = "/__api__/"
VERIFY_PREFIX = "/__verify__/"

logging.basicConfig(
    level=logging.INFO, format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("lunaris_server")

# ---------------------------------------------------------------------------
# MIME / 压缩 / 缓存
# ---------------------------------------------------------------------------

_COMPRESSIBLE = {".js", ".css", ".html", ".json", ".svg", ".txt", ".xml"}
_STATIC_BIN = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".wav", ".ico"}
_MIME_MAP = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml; charset=utf-8",
    ".webp": "image/webp", ".wav": "audio/wav",
    ".woff2": "font/woff2", ".woff": "font/woff",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".ico": "image/x-icon",
}

_CACHE_MAX_AGE: dict[str, int] = {
    # 图片/字体/音频 — 7 天
    ".png": 604800, ".jpg": 604800, ".jpeg": 604800, ".gif": 604800,
    ".webp": 604800, ".woff": 604800, ".woff2": 604800, ".wav": 604800,
    ".ico": 604800,
    # JS/CSS — 1 天
    ".js": 86400, ".css": 86400,
    # HTML/JSON — 1 小时
    ".html": 3600, ".json": 3600,
}


def _make_etag(st: os.stat_result) -> str:
    """基于 mtime 和 size 生成 ETag。"""
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def _cache_control(ext: str) -> str:
    """根据文件扩展名返回 Cache-Control 值。"""
    max_age = _CACHE_MAX_AGE.get(ext, 3600)
    return f"public, max-age={max_age}"


def _gz_sidecar(fp: Path) -> tuple[Path, os.stat_result] | None:
    """检查 .gz 旁文件是否存在且 mtime >= 原文件，返回 (gz_path, gz_stat) 或 None。"""
    gz = fp.with_suffix(fp.suffix + ".gz")
    try:
        gz_st = gz.stat()
        orig_st = fp.stat()
        if gz_st.st_mtime >= orig_st.st_mtime:
            return gz, gz_st
    except OSError:
        pass
    return None


def _has_any_gz(project_root: Path) -> bool:
    """快速检查 site/ 或 api/ 下是否存在至少一个 .gz 旁文件。"""
    for subdir in ("site", "api"):
        base = project_root / subdir
        if not base.exists():
            continue
        for ext in _COMPRESSIBLE:
            for _ in base.rglob(f"*{ext}.gz"):
                return True
    return False


def load_expected_404(path: Path | None) -> set[str]:
    """从文件加载预期的 404 URL 集合。"""
    if not path or not path.exists():
        return set()
    urls: set[str] = set()
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if ln.startswith("https://"):
            urls.add(ln)
    return urls


# ---------------------------------------------------------------------------
# stdlib 模式
# ---------------------------------------------------------------------------


class DualStackHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """多线程 + IPv4/IPv6 双栈 HTTP 服务器。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_bind(self) -> None:
        try:
            self.address_family = socket.AF_INET6
            self.socket = socket.socket(self.address_family, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            host, port = self.server_address
            self.server_address = ("::", port)
            self.socket.bind(self.server_address)
            self.server_address = self.socket.getsockname()[:2]
        except OSError:
            self.address_family = socket.AF_INET
            self.socket = socket.socket(self.address_family, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(self.server_address)
            self.server_address = self.socket.getsockname()[:2]


class LunarisServer(DualStackHTTPServer):
    """Lunaris 本地服务器（stdlib 模式）。"""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type,
        *,
        project_root: Path,
        port: int,
        rewrite: bool,
        expected_404: set[str],
    ):
        super().__init__(server_address, handler_cls)
        self.project_root = project_root
        self.site_dir = project_root / "site"
        self.api_dir = project_root / "api"
        self.port = port
        self.rewrite = rewrite
        self.expected_404 = expected_404
        self._lock = threading.Lock()
        self.stats: dict[str, Any] = {
            "start_ts": time.time(),
            "requests_total": 0,
            "served_200": 0,
            "served_304": 0,
            "served_404_expected": 0,
            "served_404_unexpected": 0,
        }
        self.unexpected_missing_urls: set[str] = set()

    def bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] = int(self.stats.get(key, 0)) + 1

    def add_unexpected(self, url: str) -> None:
        with self._lock:
            self.unexpected_missing_urls.add(url)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = time.time() - float(self.stats["start_ts"])
            return {
                **self.stats,
                "elapsed_s": round(elapsed, 1),
                "unexpected_missing_unique": len(self.unexpected_missing_urls),
            }


class LunarisHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 请求处理器，支持 gzip 缓存、ETag/304、Cache-Control、URL 重写。"""

    server_version = "Lunaris/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle_request(head_only=False)

    def do_HEAD(self) -> None:
        self._handle_request(head_only=True)

    def _handle_request(self, *, head_only: bool) -> None:
        srv: LunarisServer = self.server  # type: ignore[assignment]
        srv.bump("requests_total")

        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith("/"):
            path = "/" + path

        # 验证端点
        if path.startswith(VERIFY_PREFIX):
            self._handle_verify(path, srv, head_only=head_only)
            return

        # 解析文件
        resolved = self._resolve(srv, path)
        if resolved is None:
            self._handle_404(srv, path)
            return

        fp, ext, is_html = resolved
        try:
            st = fp.stat()
        except OSError:
            srv.bump("served_404_unexpected")
            self.send_error(404)
            return

        etag = _make_etag(st)
        cc = _cache_control(ext)

        # 304 条件请求（非重写内容才走 304）
        if_none = self.headers.get("If-None-Match")
        if if_none and (not srv.rewrite or (not is_html and ext != ".js")):
            if etag in [t.strip() for t in if_none.split(",")]:
                srv.bump("served_304")
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cc)
                self.send_header("Access-Control-Allow-Origin", "*")
                if ext in _COMPRESSIBLE:
                    self.send_header("Vary", "Accept-Encoding")
                self.end_headers()
                return

        # 读取并可能重写内容
        ctype = _MIME_MAP.get(ext, "application/octet-stream")
        if ext == "" and is_html:
            ctype = "text/html; charset=utf-8"

        if srv.rewrite and (is_html or ext == ".js"):
            text = fp.read_bytes().decode("utf-8", errors="replace")
            text = text.replace(
                "https://api.lunaris.moe",
                f"http://localhost:{srv.port}/__api__",
            )
            text = text.replace(
                "https://lunaris.moe",
                f"http://localhost:{srv.port}",
            )
            body = text.encode("utf-8")
            use_gzip = (
                ext in _COMPRESSIBLE
                and len(body) > 256
                and "gzip" in self.headers.get("Accept-Encoding", "")
            )
            if use_gzip:
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                    gz.write(body)
                body = buf.getvalue()
            gz_file = False
        else:
            # 非重写内容：优先使用 .gz 旁文件
            ae = self.headers.get("Accept-Encoding", "")
            use_gzip = False
            gz_file = False
            if ext in _COMPRESSIBLE and "gzip" in ae:
                gz_info = _gz_sidecar(fp)
                if gz_info:
                    gz_file = True
                    use_gzip = True
            if gz_file:
                body = gz_info[0].read_bytes()
            else:
                body = fp.read_bytes()

        srv.bump("served_200")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cc)
        self.send_header("Access-Control-Allow-Origin", "*")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        if ext in _COMPRESSIBLE:
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _handle_404(self, srv: LunarisServer, path: str) -> None:
        """处理 404 响应，区分预期和非预期缺失。"""
        is_api = path.startswith(API_PREFIX)
        if is_api:
            upstream = API_ORIGIN + path[len(API_PREFIX) - 1:]
        else:
            upstream = LUNARIS_ORIGIN + path

        if upstream in srv.expected_404:
            srv.bump("served_404_expected")
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("X-Lunaris-Expected-404", "1")
            body = b"Not Found (expected)\n"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            srv.bump("served_404_unexpected")
            srv.add_unexpected(upstream)
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("X-Lunaris-Local-Miss", "1")
            body = b"Not Found (missing)\n"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _handle_verify(self, path: str, srv: LunarisServer, *, head_only: bool) -> None:
        """处理 /__verify__/ 端点。"""
        sub = path[len(VERIFY_PREFIX):].lstrip("/")
        if sub in ("stats", "stats.json", ""):
            payload = json.dumps(srv.snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
        elif sub in ("unexpected-missing", "unexpected-missing.json"):
            with srv._lock:
                arr = sorted(srv.unexpected_missing_urls)
            payload = json.dumps(
                {"count": len(arr), "urls": arr}, ensure_ascii=False, indent=2,
            ).encode("utf-8")
        else:
            self.send_error(404, "Unknown verify endpoint")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _resolve(
        self, srv: LunarisServer, req_path: str,
    ) -> tuple[Path, str, bool] | None:
        """将请求路径解析为本地文件。返回 (path, ext, is_html) 或 None。"""
        raw = req_path
        decoded = urllib.parse.unquote(raw)
        for candidate in (decoded, raw):
            is_api = candidate.startswith(API_PREFIX)
            if is_api:
                root = srv.api_dir
                sub = candidate[len(API_PREFIX):]
            else:
                root = srv.site_dir
                sub = candidate.lstrip("/")

            if not sub:
                fp = root / "index.html"
                if fp.is_file():
                    return fp, fp.suffix.lower(), True
                continue

            sub_clean = sub.rstrip("/")
            fp = root / sub_clean
            if fp.is_file() and self._safe(root, fp):
                ext = fp.suffix.lower()
                is_html = (not is_api) and ext in (".html", "")
                return fp, ext, is_html

            fp2 = root / sub_clean / "index.html"
            if fp2.is_file() and self._safe(root, fp2):
                return fp2, ".html", True
        return None

    @staticmethod
    def _safe(root: Path, target: Path) -> bool:
        """路径穿越防护。"""
        try:
            target.resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    def log_message(self, fmt: str, *args: Any) -> None:
        """使用 logger 输出访问日志。"""
        logger.info(fmt, *args)


# ---------------------------------------------------------------------------
# ASGI 模式
# ---------------------------------------------------------------------------


def _make_asgi_app(
    project_root: Path,
    port: int,
    rewrite: bool,
    expected_404: set[str],
) -> Any:
    """构建 Starlette ASGI 应用，复刻 stdlib 模式的全部功能。"""
    import asyncio
    import hashlib

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, Response
    from starlette.routing import Route

    site_dir = (project_root / "site").resolve()
    api_dir = (project_root / "api").resolve()

    # 共享状态
    lock = asyncio.Lock()
    stats: dict[str, Any] = {
        "start_ts": time.time(),
        "requests_total": 0,
        "served_200": 0,
        "served_304": 0,
        "served_404_expected": 0,
        "served_404_unexpected": 0,
    }
    unexpected_missing: set[str] = set()

    async def bump(key: str) -> None:
        async with lock:
            stats[key] = int(stats.get(key, 0)) + 1

    async def snapshot() -> dict[str, Any]:
        async with lock:
            elapsed = time.time() - float(stats["start_ts"])
            return {
                **stats,
                "elapsed_s": round(elapsed, 1),
                "unexpected_missing_unique": len(unexpected_missing),
            }

    def _etag_for(st: os.stat_result) -> str:
        return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'

    def _safe(root: Path, target: Path) -> bool:
        try:
            target.resolve().relative_to(root)
            return True
        except Exception:
            return False

    def _resolve(req_path: str) -> tuple[Path, str, bool] | None:
        """将请求路径解析为本地文件。"""
        raw = req_path
        decoded = urllib.parse.unquote(raw)
        for candidate in (decoded, raw):
            is_api = candidate.startswith(API_PREFIX)
            if is_api:
                root = api_dir
                sub = candidate[len(API_PREFIX):]
            else:
                root = site_dir
                sub = candidate.lstrip("/")

            if not sub:
                fp = root / "index.html"
                if fp.is_file():
                    return fp, fp.suffix.lower(), True
                continue

            sub_clean = sub.rstrip("/")
            fp = root / sub_clean
            if fp.is_file() and _safe(root, fp):
                ext = fp.suffix.lower()
                is_html = (not is_api) and ext in (".html", "")
                return fp, ext, is_html

            fp2 = root / sub_clean / "index.html"
            if fp2.is_file() and _safe(root, fp2):
                return fp2, ".html", True
        return None

    async def handle_verify(request: Request) -> Response:
        """处理 /__verify__/ 端点。"""
        sub = request.path_params.get("path", "").lstrip("/")
        if sub in ("stats", "stats.json", ""):
            payload = json.dumps(await snapshot(), ensure_ascii=False, indent=2)
        elif sub in ("unexpected-missing", "unexpected-missing.json"):
            async with lock:
                arr = sorted(unexpected_missing)
            payload = json.dumps(
                {"count": len(arr), "urls": arr}, ensure_ascii=False, indent=2,
            )
        else:
            return Response(status_code=404, content="Unknown verify endpoint\n")

        return Response(
            content=payload,
            media_type="application/json; charset=utf-8",
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    async def handle_file(request: Request) -> Response:
        """处理静态文件请求。"""
        await bump("requests_total")
        path = "/" + request.path_params.get("path", "")

        resolved = _resolve(path)
        if resolved is None:
            # 404 处理
            is_api = path.startswith(API_PREFIX)
            upstream = (
                (API_ORIGIN + path[len(API_PREFIX) - 1:])
                if is_api
                else (LUNARIS_ORIGIN + path)
            )
            if upstream in expected_404:
                await bump("served_404_expected")
                return Response(
                    status_code=404,
                    content="Not Found (expected)\n",
                    headers={
                        "X-Lunaris-Expected-404": "1",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
            else:
                await bump("served_404_unexpected")
                async with lock:
                    unexpected_missing.add(upstream)
                return Response(
                    status_code=404,
                    content="Not Found (missing)\n",
                    headers={
                        "X-Lunaris-Local-Miss": "1",
                        "Access-Control-Allow-Origin": "*",
                    },
                )

        fp, ext, is_html = resolved
        try:
            st = fp.stat()
        except OSError:
            await bump("served_404_unexpected")
            return Response(status_code=404)

        etag = _etag_for(st)
        cc = _cache_control(ext)
        extra_headers: dict[str, str] = {
            "ETag": etag,
            "Cache-Control": cc,
            "Access-Control-Allow-Origin": "*",
        }
        if ext in _COMPRESSIBLE:
            extra_headers["Vary"] = "Accept-Encoding"

        # 304 条件请求
        if_none = request.headers.get("if-none-match", "")
        if if_none and (not rewrite or (not is_html and ext != ".js")):
            if etag in [t.strip() for t in if_none.split(",")]:
                await bump("served_304")
                return Response(status_code=304, headers=extra_headers)

        # 读取内容
        ctype = _MIME_MAP.get(ext, "application/octet-stream")
        if ext == "" and is_html:
            ctype = "text/html; charset=utf-8"

        if rewrite and (is_html or ext == ".js"):
            # 重写内容：运行时替换 + gzip
            text = fp.read_bytes().decode("utf-8", errors="replace")
            text = text.replace(
                "https://api.lunaris.moe",
                f"http://localhost:{port}/__api__",
            )
            text = text.replace(
                "https://lunaris.moe",
                f"http://localhost:{port}",
            )
            body = text.encode("utf-8")
            ae = request.headers.get("accept-encoding", "")
            if ext in _COMPRESSIBLE and len(body) > 256 and "gzip" in ae:
                body = gzip.compress(body, compresslevel=6)
                extra_headers["Content-Encoding"] = "gzip"
            await bump("served_200")
            return Response(
                content=body,
                media_type=ctype,
                headers=extra_headers,
            )

        # 非重写内容：优先使用 .gz 旁文件
        ae = request.headers.get("accept-encoding", "")
        if ext in _COMPRESSIBLE and "gzip" in ae:
            gz_info = _gz_sidecar(fp)
            if gz_info:
                gz_path, gz_st = gz_info
                await bump("served_200")
                return FileResponse(
                    gz_path,
                    stat_result=gz_st,
                    headers={
                        **extra_headers,
                        "Content-Encoding": "gzip",
                        "Content-Type": ctype,
                    },
                )

        await bump("served_200")
        return FileResponse(fp, stat_result=st, headers=extra_headers)

    return Starlette(
        routes=[
            Route("/__verify__/{path:path}", handle_verify),
            Route("/__verify__/", handle_verify),
            Route("/{path:path}", handle_file),
            Route("/", handle_file),
        ],
    )


def _serve_asgi(
    project_root: Path,
    port: int,
    bind: str,
    rewrite: bool,
    expected_404: set[str],
    *,
    workers: int | None = None,
    backlog: int = 512,
    access_log: str | None = None,
) -> None:
    """使用 hypercorn + starlette 启动 ASGI 服务器。

    注意：当 workers > 1 时，此函数不应被调用，应使用命令行模式。
    """
    import asyncio

    from hypercorn.asyncio import serve as hyper_serve
    from hypercorn.config import Config as HyperConfig

    app = _make_asgi_app(project_root, port, rewrite, expected_404)
    config = HyperConfig()
    # IPv4 + IPv6 双栈
    config.bind = [f"{bind}:{port}", f"[::]:{port}"]
    config.backlog = backlog
    # access log: 默认关闭
    if access_log == "-":
        config.accesslog = "-"
    elif access_log:
        config.accesslog = access_log
    else:
        config.accesslog = None
    asyncio.run(hyper_serve(app, config))


def _serve_stdlib(
    project_root: Path,
    port: int,
    bind: str,
    rewrite: bool,
    expected_404: set[str],
) -> None:
    """使用 stdlib http.server 启动服务器。"""
    with LunarisServer(
        (bind, port), LunarisHandler,
        project_root=project_root, port=port, rewrite=rewrite, expected_404=expected_404,
    ) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def serve(
    *,
    project_root: Path,
    port: int,
    bind: str,
    rewrite: bool,
    expected_404: set[str],
    workers: int | None = None,
    backlog: int = 512,
    access_log: str | None = None,
) -> None:
    """启动本地服务器，自动检测最佳引擎。"""
    has_asgi = False
    try:
        import starlette  # noqa: F401
        import hypercorn  # noqa: F401
        has_asgi = True
    except ImportError:
        pass

    # 多进程模式：使用 hypercorn 命令行
    if has_asgi and workers and workers > 1:
        import shutil
        import subprocess

        hypercorn_bin = shutil.which("hypercorn")
        if not hypercorn_bin:
            logger.error(
                "错误: 找不到 hypercorn 命令。请确保已安装:\n"
                "  pip install hypercorn"
            )
            sys.exit(1)

        # 检查 .gz 预压缩文件
        if not _has_any_gz(project_root):
            logger.warning(
                "⚠ 未检测到 .gz 预压缩文件，文本资源将不会被压缩发送。\n"
                "  运行 `python lunaris.py precompress` 生成 .gz 文件以启用压缩。"
            )

        logger.info("=" * 50)
        logger.info("Lunaris 服务器")
        logger.info("=" * 50)
        logger.info(f"引擎: hypercorn (multi-process, {workers} workers)")
        logger.info(f"项目目录: {project_root}")
        logger.info(f"监听地址: http://{bind or '0.0.0.0'}:{port}/")
        logger.info(f"API 前缀: http://localhost:{port}/__api__/")
        logger.info(f"URL 重写: {'ON' if rewrite else 'OFF'}")
        logger.info(f"Expected 404: {len(expected_404)} 条")
        logger.info(f"统计接口: http://localhost:{port}/__verify__/stats")
        logger.info("=" * 50)

        # 构建命令行参数
        cmd = [
            hypercorn_bin,
            "lunaris.server:app",
            "--bind", f"{bind}:{port}",
            "--bind", f"[::]:{port}",
            "--workers", str(workers),
            "--backlog", str(backlog),
        ]
        if access_log == "-":
            cmd.append("--access-log")
            cmd.append("-")
        elif access_log:
            cmd.extend(["--access-logfile", access_log])

        try:
            subprocess.run(cmd, check=True, cwd=project_root)
        except KeyboardInterrupt:
            logger.info("\n服务器已停止。")
        except subprocess.CalledProcessError as e:
            logger.error(f"Hypercorn exited with code {e.returncode}")
            sys.exit(e.returncode)
        return

    # 单进程模式
    if has_asgi:
        engine = "hypercorn + starlette (ASGI)"
    else:
        engine = "http.server (stdlib)"

    # 检查 .gz 预压缩文件
    if not _has_any_gz(project_root):
        logger.warning(
            "⚠ 未检测到 .gz 预压缩文件，文本资源将不会被压缩发送。\n"
            "  运行 `python lunaris.py precompress` 生成 .gz 文件以启用压缩。"
        )

    logger.info("=" * 50)
    logger.info("Lunaris 服务器")
    logger.info("=" * 50)
    logger.info(f"引擎: {engine}")
    logger.info(f"项目目录: {project_root}")
    logger.info(f"监听地址: http://{bind or '0.0.0.0'}:{port}/")
    logger.info(f"API 前缀: http://localhost:{port}/__api__/")
    logger.info(f"URL 重写: {'ON' if rewrite else 'OFF'}")
    logger.info(f"Expected 404: {len(expected_404)} 条")
    logger.info(f"统计接口: http://localhost:{port}/__verify__/stats")
    if not has_asgi:
        logger.info("提示: pip install starlette hypercorn 可启用高性能 ASGI 模式")
    logger.info("=" * 50)

    if has_asgi:
        _serve_asgi(
            project_root, port, bind, rewrite, expected_404,
            workers=None, backlog=backlog, access_log=access_log,
        )
    else:
        _serve_stdlib(project_root, port, bind, rewrite, expected_404)


# ---------------------------------------------------------------------------
# 模块级 ASGI app，供 hypercorn 命令行使用
# ---------------------------------------------------------------------------

# 从环境变量读取配置（用于命令行模式）
_PROJECT_ROOT = Path(os.environ.get("LUNARIS_PROJECT_ROOT", Path.cwd()))
_PORT = int(os.environ.get("LUNARIS_PORT", "9000"))
_REWRITE = os.environ.get("LUNARIS_REWRITE", "1") == "1"

# 加载 expected_404
_expected_404: set[str] = set()
try:
    _expected_404 |= load_expected_404(_PROJECT_ROOT / "logs" / "scan" / "remaining_unavailable.txt")
    _expected_404 |= load_expected_404(_PROJECT_ROOT / "logs" / "audit" / "expected_upstream_404.txt")
except Exception:
    pass

# 创建模块级 app（仅在安装了 starlette 时）
app = None
try:
    import starlette  # noqa: F401
    if (_PROJECT_ROOT / "site").exists() or (_PROJECT_ROOT / "api").exists():
        app = _make_asgi_app(_PROJECT_ROOT, _PORT, _REWRITE, _expected_404)
except ImportError:
    pass
