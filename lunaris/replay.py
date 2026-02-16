"""Phase 2: Playwright 动态回放 — 访问页面捕获运行时网络请求，补抓遗漏 URL。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .common import (
    DEFAULT_CONCURRENCY, LUNARIS_ORIGIN, UA,
    Downloader, JsonlWriter,
    build_all_routes, is_target_url, load_existing_urls, normalize_url,
)


async def run(project_root: Path, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    """访问采样页面，捕获运行时网络请求并下载新发现的 URL。"""
    from playwright.async_api import async_playwright

    manifest_path = project_root / "manifest.jsonl"
    existing_urls = load_existing_urls(manifest_path)
    manifest = JsonlWriter(manifest_path)
    dl = Downloader(project_root=project_root, manifest=manifest, concurrency=concurrency)

    captured_urls: set[str] = set()
    pending: set[asyncio.Task[Any]] = set()

    def schedule(coro: Any) -> None:
        task = asyncio.create_task(coro)
        pending.add(task)
        task.add_done_callback(lambda t: pending.discard(t))

    async def on_response(resp: Any) -> None:
        url = normalize_url(resp.url)
        if is_target_url(url):
            captured_urls.add(url)

    routes = build_all_routes(project_root)

    # 采样：每个类别最多取前 50 个
    sampled: list[str] = []
    counts: dict[str, int] = {}
    for r in routes:
        prefix = r.split("/")[1] if "/" in r.lstrip("/") else r
        if prefix in ("character", "weapon", "artifact", "material"):
            counts[prefix] = counts.get(prefix, 0) + 1
            if counts[prefix] > 50:
                continue
        sampled.append(r)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=UA, viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()
            page.on("response", lambda resp: schedule(on_response(resp)))

            for route in sampled:
                url = f"{LUNARIS_ORIGIN}{route}"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(2000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass  # 单页导航失败不中断整体流程

            await page.wait_for_timeout(3000)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await context.close()
            await browser.close()

        # 下载新发现的 URL
        new_urls = sorted(
            u for u in captured_urls
            if normalize_url(u) not in existing_urls and is_target_url(u)
        )
        for i in range(0, len(new_urls), 100):
            await asyncio.gather(*(dl.fetch(u) for u in new_urls[i:i + 100]))

        print(f"[replay] 捕获 {len(captured_urls)} URL，新增下载 {len(new_urls)}")
    finally:
        await dl.close()
        await manifest.close()
