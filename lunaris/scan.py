"""Phase 4: Playwright 全站扫描 — 逐页访问，记录加载失败和本地缺失，然后补抓。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .common import (
    DEFAULT_CONCURRENCY, LUNARIS_ORIGIN, UA,
    Downloader, JsonlWriter, LineSet, PageNetState,
    build_all_routes, is_target_url, local_file_exists,
    normalize_url, now_ts, should_skip_url, wait_network_quiet,
)

WORKERS = int(os.environ.get("LUNARIS_SCAN_WORKERS", "4"))
NAV_TIMEOUT_MS = 45_000
QUIET_TIMEOUT_MS = 4_500
QUIET_WINDOW_MS = 400


def _is_noise(url: str) -> bool:
    """判断 URL 是否为 CDN 噪声请求。"""
    try:
        s = urlsplit(url)
    except Exception:
        return True
    return s.path.startswith("/cdn-cgi/")


def _is_relevant(url: str) -> bool:
    """判断 URL 是否为需要追踪的目标请求。"""
    u = normalize_url(url)
    return is_target_url(u) and not _is_noise(u)


def _bind_page_events(
    page: Any,
    state: PageNetState,
    project_root: Path,
    failures: JsonlWriter,
    missing_local: JsonlWriter,
    missing_urls: set[str],
    missing_lock: asyncio.Lock,
) -> None:
    """绑定页面网络事件监听器。"""

    def on_request(req: Any) -> None:
        if _is_relevant(req.url):
            state.inc()

    def on_request_finished(req: Any) -> None:
        if _is_relevant(req.url):
            state.dec()

    def on_request_failed(req: Any) -> None:
        u = normalize_url(req.url)
        if not is_target_url(u) or _is_noise(u):
            state.dec()
            return
        state.dec()
        err = str(req.failure) if req.failure else None
        asyncio.create_task(failures.write({
            "ts": now_ts(), "kind": "request_failed",
            "route": state.current_route, "url": u, "error": err,
        }))

    def on_response(resp: Any) -> None:
        u = normalize_url(resp.url)
        if not is_target_url(u) or _is_noise(u):
            return
        state.touch()
        if resp.status >= 400 and resp.request.method == "GET":
            asyncio.create_task(failures.write({
                "ts": now_ts(), "kind": "http_error",
                "route": state.current_route, "url": u, "status": resp.status,
            }))
        if resp.status == 200 and resp.request.method == "GET":
            if not local_file_exists(project_root, u):
                asyncio.create_task(missing_local.write({
                    "ts": now_ts(), "route": state.current_route, "url": u,
                }))

                async def _add() -> None:
                    async with missing_lock:
                        missing_urls.add(u)

                asyncio.create_task(_add())

    page.on("request", on_request)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


async def _scan_worker(
    wid: int,
    pages: list[Any],
    states: list[PageNetState],
    q: asyncio.Queue[str],
    completed: LineSet,
) -> None:
    """单个扫描 worker：从队列取路由，访问页面并等待网络平静。"""
    while True:
        try:
            route = q.get_nowait()
        except asyncio.QueueEmpty:
            return
        state = states[wid]
        state.current_route = route
        state.current_url = f"{LUNARIS_ORIGIN}{route}"

        for attempt in range(1, 4):
            try:
                await pages[wid].goto(
                    state.current_url,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
                try:
                    await pages[wid].evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)"
                    )
                    await pages[wid].wait_for_timeout(150)
                    await pages[wid].evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass  # 滚动失败不影响扫描
                await wait_network_quiet(
                    pages[wid], state,
                    timeout_ms=QUIET_TIMEOUT_MS, window_ms=QUIET_WINDOW_MS,
                )
                break
            except Exception:
                await pages[wid].wait_for_timeout(300 * attempt)

        await completed.add(route)
        q.task_done()


async def run(project_root: Path, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    """执行全站 Playwright 扫描，记录失败和缺失，然后补抓。"""
    from playwright.async_api import async_playwright

    routes = build_all_routes(project_root)
    logs_dir = project_root / "logs" / "scan"
    logs_dir.mkdir(parents=True, exist_ok=True)

    completed = LineSet(logs_dir / "completed_routes.txt")
    done = completed.load()
    remaining = [r for r in routes if r not in done]
    if not remaining:
        print("[scan] 所有路由已扫描完毕")
        return

    failures = JsonlWriter(logs_dir / "failures.jsonl")
    missing_local = JsonlWriter(logs_dir / "missing_local.jsonl")
    missing_urls: set[str] = set()
    missing_lock = asyncio.Lock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900},
        )

        async def route_handler(route_obj: Any, request: Any) -> None:
            if _is_noise(normalize_url(request.url)):
                await route_obj.abort()
            else:
                await route_obj.continue_()

        await context.route("**/*", route_handler)

        q: asyncio.Queue[str] = asyncio.Queue()
        for r in remaining:
            q.put_nowait(r)

        worker_count = max(1, min(WORKERS, 8))
        pages: list[Any] = []
        states: list[PageNetState] = []

        for _ in range(worker_count):
            pg = await context.new_page()
            st = PageNetState(last_event_mono=__import__("time").monotonic())
            _bind_page_events(
                pg, st, project_root,
                failures, missing_local, missing_urls, missing_lock,
            )
            pages.append(pg)
            states.append(st)

        tasks = [
            asyncio.create_task(_scan_worker(i, pages, states, q, completed))
            for i in range(worker_count)
        ]
        await asyncio.gather(*tasks)
        await context.close()
        await browser.close()

    await failures.close()
    await missing_local.close()

    # 补抓缺失文件
    to_fix = sorted(
        u for u in missing_urls
        if not local_file_exists(project_root, u) and not should_skip_url(u)
    )
    if to_fix:
        manifest = JsonlWriter(project_root / "manifest.jsonl")
        dl = Downloader(project_root=project_root, manifest=manifest, concurrency=concurrency)
        try:
            for i in range(0, len(to_fix), 120):
                batch = to_fix[i:i + 120]
                await asyncio.gather(*(dl.fetch(u) for u in batch))
                done_n = min(i + 120, len(to_fix))
                print(f"[scan fix] {done_n}/{len(to_fix)}", file=sys.stderr)
        finally:
            await dl.close()
            await manifest.close()

    print(f"[scan] 扫描 {len(routes)} 路由，发现缺失 {len(missing_urls)}，补抓 {len(to_fix)}")