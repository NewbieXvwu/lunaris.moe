"""Phase 5: 离线 UI 交互审计 — 点击 tab/展开折叠/轮询 select，补抓懒加载资源。"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .common import (
    API_ORIGIN, DEFAULT_CONCURRENCY, LUNARIS_ORIGIN, UA,
    Downloader, JsonlWriter, LineSet, PageNetState,
    build_all_routes, local_file_exists, normalize_url, now_ts,
    url_to_relpath, wait_network_quiet,
)

API_PREFIX = "/__api__/"
DEFAULT_WORKERS = int(os.environ.get("LUNARIS_UI_AUDIT_WORKERS", "8"))
NAV_TIMEOUT_MS = 45_000
QUIET_TIMEOUT_MS = 6_000
QUIET_WINDOW_MS = 500
MAX_TABS = 32
MAX_EXPAND_BTNS = 64


# ---------------------------------------------------------------------------
# URL 工具
# ---------------------------------------------------------------------------


def _local_to_upstream(url: str) -> str:
    """将本地服务器 URL 转换为上游 URL。"""
    s = urlsplit(url)
    path = s.path
    q = ("?" + s.query) if s.query else ""
    if path.startswith(API_PREFIX):
        return f"{API_ORIGIN}{path[len(API_PREFIX) - 1:]}{q}"
    return f"{LUNARIS_ORIGIN}{path}{q}"


def _is_local(url: str, netlocs: set[str]) -> bool:
    """判断 URL 是否指向本地服务器。"""
    try:
        s = urlsplit(url)
    except Exception:
        return False
    return s.scheme in {"http", "https"} and s.netloc in netlocs


# ---------------------------------------------------------------------------
# UI 交互
# ---------------------------------------------------------------------------


async def _scroll(page: Any) -> None:
    """逐步滚动页面以触发懒加载。"""
    try:
        for frac in (0.25, 0.5, 0.75, 1.0):
            await page.evaluate(
                "f => { const h = Math.max(document.body.scrollHeight,"
                " document.documentElement.scrollHeight); window.scrollTo(0, h*f); }",
                frac,
            )
            await page.wait_for_timeout(120)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(80)
    except Exception:
        pass  # 页面可能已导航离开


async def _exercise(page: Any, state: PageNetState) -> None:
    """在页面上执行 UI 交互：点击 tab、展开折叠、滚动。"""
    try:
        main = page.locator("main")
        scope = main if await main.count() > 0 else page.locator("body")
    except Exception:
        scope = page.locator("body")

    await _scroll(page)

    # 点击 tab
    tabs = scope.get_by_role("tab")
    try:
        n = min(await tabs.count(), MAX_TABS)
    except Exception:
        n = 0
    for i in range(n):
        tab = tabs.nth(i)
        try:
            sel = await tab.get_attribute("aria-selected")
        except Exception:
            sel = None
        if sel == "true":
            continue
        try:
            await tab.scroll_into_view_if_needed(timeout=1000)
            await tab.click(timeout=2500)
        except Exception:
            continue
        await page.wait_for_timeout(80)
        await wait_network_quiet(page, state, timeout_ms=QUIET_TIMEOUT_MS, window_ms=QUIET_WINDOW_MS)
        await _scroll(page)

    # 点击 tab-like 按钮
    btns = page.locator("button.rounded-xl.text-sm.font-semibold.tracking-wide")
    try:
        n = min(await btns.count(), MAX_TABS)
    except Exception:
        n = 0
    for i in range(n):
        try:
            await btns.nth(i).scroll_into_view_if_needed(timeout=1000)
            await btns.nth(i).click(timeout=2500)
        except Exception:
            continue
        await page.wait_for_timeout(80)
        await wait_network_quiet(page, state, timeout_ms=QUIET_TIMEOUT_MS, window_ms=QUIET_WINDOW_MS)
        await _scroll(page)

    # 展开折叠
    exp_btns = scope.locator("button[aria-expanded='false']")
    try:
        n = min(await exp_btns.count(), MAX_EXPAND_BTNS)
    except Exception:
        n = 0
    for i in range(n):
        try:
            await exp_btns.nth(i).scroll_into_view_if_needed(timeout=1000)
            await exp_btns.nth(i).click(timeout=2000)
        except Exception:
            continue
        await page.wait_for_timeout(60)
        await wait_network_quiet(page, state, timeout_ms=QUIET_TIMEOUT_MS, window_ms=QUIET_WINDOW_MS)

    try:
        await scope.locator("details:not([open])").evaluate_all(
            "els => els.forEach(e => { e.open = true; })"
        )
    except Exception:
        pass  # 没有 <details> 元素

    await wait_network_quiet(page, state, timeout_ms=QUIET_TIMEOUT_MS, window_ms=QUIET_WINDOW_MS)


# ---------------------------------------------------------------------------
# 本地服务器管理
# ---------------------------------------------------------------------------


def _wait_port(host: str, port: int, timeout_s: float = 10.0) -> None:
    """等待端口就绪。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"端口未就绪: {host}:{port}")


def _start_server(project_root: Path, port: int) -> tuple[threading.Thread, Any]:
    """启动本地验证服务器并等待就绪。"""
    from .server import LunarisHandler, LunarisServer, load_expected_404

    expected = load_expected_404(project_root / "logs" / "scan" / "remaining_unavailable.txt")
    expected |= load_expected_404(project_root / "logs" / "audit" / "expected_upstream_404.txt")
    httpd = LunarisServer(
        ("127.0.0.1", port), LunarisHandler,
        project_root=project_root, port=port, rewrite=True, expected_404=expected,
    )
    t = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=0.25),
        daemon=True,
    )
    t.start()
    _wait_port("127.0.0.1", port)
    return t, httpd


# ---------------------------------------------------------------------------
# 事件绑定 + Worker
# ---------------------------------------------------------------------------


def _bind_page_events(
    page: Any,
    state: PageNetState,
    allowed_netlocs: set[str],
    missing_set: set[str],
    missing_lock: asyncio.Lock,
) -> None:
    """绑定页面网络事件监听器，追踪请求状态和缺失资源。"""

    def on_request(req: Any) -> None:
        if _is_local(req.url, allowed_netlocs):
            state.inc()

    def on_request_finished(req: Any) -> None:
        if _is_local(req.url, allowed_netlocs):
            state.dec()

    def on_request_failed(req: Any) -> None:
        if _is_local(req.url, allowed_netlocs):
            state.dec()

    def on_response(resp: Any) -> None:
        if not _is_local(resp.url, allowed_netlocs):
            return
        state.touch()
        if resp.request.method != "GET" or resp.status != 404:
            return
        h = {k.lower(): v for k, v in (resp.headers or {}).items()}
        if h.get("x-lunaris-local-miss") != "1":
            return
        upstream = _local_to_upstream(resp.url)

        async def _rec() -> None:
            async with missing_lock:
                missing_set.add(upstream)

        asyncio.create_task(_rec())

    page.on("request", on_request)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


async def _audit_worker(
    wid: int,
    page_list: list[Any],
    state_list: list[PageNetState],
    q: asyncio.Queue[str],
    completed: LineSet,
    base_origin: str,
) -> None:
    """单个审计 worker：访问页面并执行 UI 交互。"""
    while True:
        try:
            route = q.get_nowait()
        except asyncio.QueueEmpty:
            return
        state = state_list[wid]
        state.current_route = route
        state.current_url = f"{base_origin}{route}"

        for attempt in range(1, 4):
            try:
                await page_list[wid].goto(
                    state.current_url,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
                await page_list[wid].wait_for_timeout(60)
                await wait_network_quiet(
                    page_list[wid], state,
                    timeout_ms=QUIET_TIMEOUT_MS, window_ms=QUIET_WINDOW_MS,
                )
                await _exercise(page_list[wid], state)
                break
            except Exception:
                await page_list[wid].wait_for_timeout(250 * attempt)

        await completed.add(route)
        q.task_done()


async def _fix_missing(
    missing_set: set[str],
    project_root: Path,
    out_dir: Path,
    concurrency: int,
) -> list[str]:
    """补抓缺失文件并记录上游 404，返回实际补抓的 URL 列表。"""
    to_fix = sorted(
        u for u in missing_set
        if not local_file_exists(project_root, normalize_url(u))
    )
    if not to_fix:
        return []

    manifest = JsonlWriter(project_root / "manifest.jsonl")
    dl = Downloader(project_root=project_root, manifest=manifest, concurrency=concurrency)
    try:
        results = []
        for i in range(0, len(to_fix), 200):
            batch = to_fix[i:i + 200]
            results.extend(await asyncio.gather(*(dl.fetch(u) for u in batch)))
            print(
                f"[audit fix] {min(i + 200, len(to_fix))}/{len(to_fix)}",
                file=sys.stderr,
            )

        # 记录上游 404
        new_404 = sorted(r.url for r in results if not r.ok and r.status_code == 404)
        if new_404:
            exp_path = out_dir / "expected_upstream_404.txt"
            existing: set[str] = set()
            if exp_path.exists():
                existing = {
                    ln.strip()
                    for ln in exp_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip()
                }
            exp_path.write_text(
                "\n".join(sorted(existing | set(new_404))) + "\n",
                encoding="utf-8",
            )
    finally:
        await dl.close()
        await manifest.close()

    return to_fix


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def run(
    project_root: Path,
    port: int = 9001,
    workers: int = DEFAULT_WORKERS,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_rounds: int = 2,
) -> None:
    """执行 UI 交互审计：启动本地服务器，用 Playwright 逐页交互，补抓懒加载资源。"""
    from playwright.async_api import async_playwright

    routes = build_all_routes(project_root)
    out_dir = project_root / "logs" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    base_origin = f"http://127.0.0.1:{port}"
    allowed_netlocs = {f"127.0.0.1:{port}", f"localhost:{port}"}

    for rnd in range(1, max_rounds + 1):
        completed = LineSet(out_dir / "completed_routes.txt")
        if completed.path.exists():
            try:
                completed.path.unlink()
            except OSError:
                pass

        t, httpd = _start_server(project_root, port)
        missing_set: set[str] = set()
        missing_lock = asyncio.Lock()

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=UA, viewport={"width": 1440, "height": 900},
                )
                q: asyncio.Queue[str] = asyncio.Queue()
                for r in routes:
                    q.put_nowait(r)

                worker_n = max(1, min(workers, 16))
                page_list: list[Any] = []
                state_list: list[PageNetState] = []

                for _ in range(worker_n):
                    pg = await context.new_page()
                    st = PageNetState(last_event_mono=time.monotonic())
                    _bind_page_events(pg, st, allowed_netlocs, missing_set, missing_lock)
                    page_list.append(pg)
                    state_list.append(st)

                tasks = [
                    asyncio.create_task(
                        _audit_worker(i, page_list, state_list, q, completed, base_origin)
                    )
                    for i in range(worker_n)
                ]
                await asyncio.gather(*tasks)
                await context.close()
                await browser.close()
        finally:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
            t.join(timeout=2.0)

        to_fix = await _fix_missing(missing_set, project_root, out_dir, concurrency)
        print(f"[audit] 第 {rnd} 轮：扫描 {len(routes)} 路由，缺失 {len(missing_set)}，补抓 {len(to_fix)}")
        if not to_fix:
            break