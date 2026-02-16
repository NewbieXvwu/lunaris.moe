"""Phase 3: 闭包验证 — 从已下载文件中提取 URL，补抓缺失资源。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .common import (
    DEFAULT_CONCURRENCY,
    Downloader, JsonlWriter,
    collect_json_semantic_urls, extract_urls_from_text,
    is_target_url, load_existing_urls, local_file_exists,
    safe_read_text, should_skip_url,
)


def _collect_from_text_files(project_root: Path) -> set[str]:
    """扫描 site/ 和 api/ 下的文本文件，提取 URL。"""
    out: set[str] = set()
    for subdir in ("site", "api"):
        base = project_root / subdir
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in (".html", ".js", ".css", ".json", ".txt"):
                continue
            text = safe_read_text(p)
            if text:
                for u in extract_urls_from_text(text):
                    if is_target_url(u) and not should_skip_url(u):
                        out.add(u)
    return out


def _collect_all_json_files(project_root: Path) -> list[Path]:
    """收集 site/ 和 api/ 下的所有 JSON 文件。"""
    sources: list[Path] = []
    for subdir in ("site", "api"):
        base = project_root / subdir
        if not base.exists():
            continue
        sources.extend(p for p in base.rglob("*.json") if p.is_file())
    return sources


async def run(project_root: Path, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    """执行闭包验证：从已下载文件中提取 URL 并补抓缺失资源。"""
    manifest_path = project_root / "manifest.jsonl"
    existing = load_existing_urls(manifest_path)
    manifest = JsonlWriter(manifest_path)
    dl = Downloader(project_root=project_root, manifest=manifest, concurrency=concurrency)

    try:
        text_urls = _collect_from_text_files(project_root)
        json_sources = _collect_all_json_files(project_root)
        semantic_urls = collect_json_semantic_urls(json_sources, include_well_known=True)
        candidates = text_urls | semantic_urls

        missing = sorted(u for u in candidates if not local_file_exists(project_root, u))
        to_fetch = [u for u in missing if u not in existing]

        for i in range(0, len(to_fetch), 120):
            await asyncio.gather(*(dl.fetch(u) for u in to_fetch[i:i + 120]))

        still_missing = sum(1 for u in candidates if not local_file_exists(project_root, u))
        print(f"[closure] 候选 {len(candidates)}，缺失 {len(missing)}，下载 {len(to_fetch)}，仍缺 {still_missing}")
    finally:
        await dl.close()
        await manifest.close()
