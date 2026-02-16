"""Phase 1: 主备份 — HTTP 爬取前端 + API 全部已知资源。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

from .common import (
    API_ORIGIN, DEFAULT_LANGS, LUNARIS_ORIGIN,
    Downloader, JsonlWriter,
    collect_json_semantic_urls, extract_urls_from_text,
    is_target_url, normalize_url, read_json, url_to_relpath,
)


async def _fetch_seeds(dl: Downloader) -> None:
    """Phase 1a: 下载种子 URL（首页、sitemap、语言包、API 列表等）。"""
    seeds: list[str] = [
        f"{LUNARIS_ORIGIN}/",
        f"{LUNARIS_ORIGIN}/robots.txt",
        f"{LUNARIS_ORIGIN}/sitemap.xml",
        f"{LUNARIS_ORIGIN}/favicon.ico",
        f"{LUNARIS_ORIGIN}/data/banners.json",
        f"{LUNARIS_ORIGIN}/data/changelog/charlist.json",
        f"{LUNARIS_ORIGIN}/data/changelog/weaponlist.json",
        f"{LUNARIS_ORIGIN}/data/changelog/artifactlist.json",
    ]
    for lang in ["EN", "CHS", "RU", "PT"]:
        seeds.append(f"{LUNARIS_ORIGIN}/data/languages/{lang}.json")
    for lid in [5269001, 5269002, 5269003, 5269004, 5269005, 5269006, 5269007]:
        seeds.append(f"{LUNARIS_ORIGIN}/data/leylinechallenge/{lid}.json")
    seeds += [
        f"{API_ORIGIN}/data/version.json",
        f"{API_ORIGIN}/data/changelog/charlist.json",
        f"{API_ORIGIN}/data/changelog/weaponlist.json",
        f"{API_ORIGIN}/data/changelog/artifactlist.json",
        f"{API_ORIGIN}/data/latest/charlist.json",
        f"{API_ORIGIN}/data/latest/weaponlist.json",
        f"{API_ORIGIN}/data/latest/materiallist.json",
        f"{API_ORIGIN}/data/latest/artifactlist.json",
    ]
    await asyncio.gather(*(dl.fetch(u) for u in seeds))


async def _discover_frontend(dl: Downloader, project_root: Path) -> None:
    """Phase 1b-1c: 从 index.html 和 CSS 发现并下载前端资源。"""
    index_path = project_root / url_to_relpath(f"{LUNARIS_ORIGIN}/")
    index_text = index_path.read_text(encoding="utf-8", errors="replace")

    # 从 HTML 中提取 src/href 引用
    extra: set[str] = set()
    for m in re.finditer(r'\b(?:src|href)="(/[^"]+)"', index_text):
        p = m.group(1)
        if p.startswith("/assets/") or p.startswith("/fonts/"):
            extra.add(f"{LUNARIS_ORIGIN}{p}")
    extra |= extract_urls_from_text(index_text)
    await asyncio.gather(*(dl.fetch(u) for u in sorted(extra)))

    # CSS 引用的字体/资源
    css_urls: set[str] = set()
    for m in re.finditer(r'<link[^>]+href="(/assets/[^"]+\.css)"', index_text):
        css_urls.add(f"{LUNARIS_ORIGIN}{m.group(1)}")
    await asyncio.gather(*(dl.fetch(u) for u in sorted(css_urls)))

    for css_url in css_urls:
        css_path = project_root / url_to_relpath(css_url)
        if not css_path.exists():
            continue
        css_text = css_path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"url\((/[^\)]+)\)", css_text):
            p = m.group(1).strip().strip("\"'")
            if p.startswith("/fonts/") or p.startswith("/assets/"):
                await dl.fetch(f"{LUNARIS_ORIGIN}{p}")


async def _fetch_api_data(dl: Downloader, project_root: Path) -> None:
    """Phase 2: 下载 API 版本化列表和角色详情 JSON。"""
    ver_path = project_root / url_to_relpath(f"{API_ORIGIN}/data/version.json")
    api_version = read_json(ver_path)
    versions: list[str] = []
    if isinstance(api_version, dict):
        versions = [str(v) for v in api_version.get("versions", [])]
        cur = api_version.get("version")
        if cur and str(cur) not in versions:
            versions.insert(0, str(cur))

    # 版本化列表
    list_names = ["charlist", "weaponlist", "materiallist", "artifactlist"]
    ver_urls = [
        f"{API_ORIGIN}/data/{v}/{ln}.json"
        for v in versions for ln in list_names
    ]
    await asyncio.gather(*(dl.fetch(u) for u in ver_urls))

    # 角色详情
    char_urls: list[str] = []
    for ver in versions:
        cl_path = project_root / url_to_relpath(f"{API_ORIGIN}/data/{ver}/charlist.json")
        if not cl_path.exists():
            continue
        try:
            cl = read_json(cl_path)
        except Exception:
            continue
        if not isinstance(cl, dict):
            continue
        for cid in cl:
            if not str(cid).isdigit():
                continue
            for lang in DEFAULT_LANGS:
                char_urls.append(f"{API_ORIGIN}/data/{ver}/{lang}/char/{cid}.json")

    for i in range(0, len(char_urls), 300):
        await asyncio.gather(*(dl.fetch(u) for u in char_urls[i:i + 300]))


def _gather_json_sources(project_root: Path) -> list[Path]:
    """收集所有需要语义扫描的 JSON 文件路径。"""
    ver_path = project_root / url_to_relpath(f"{API_ORIGIN}/data/version.json")
    api_version = read_json(ver_path)
    versions: list[str] = []
    if isinstance(api_version, dict):
        versions = [str(v) for v in api_version.get("versions", [])]
        cur = api_version.get("version")
        if cur and str(cur) not in versions:
            versions.insert(0, str(cur))

    list_names = ["charlist", "weaponlist", "materiallist", "artifactlist"]
    sources: list[Path] = []

    for ln in list_names:
        sources.append(project_root / url_to_relpath(f"{API_ORIGIN}/data/latest/{ln}.json"))
    for ver in versions:
        for ln in list_names:
            sources.append(project_root / url_to_relpath(f"{API_ORIGIN}/data/{ver}/{ln}.json"))
    for lid in [5269001, 5269002, 5269003, 5269004, 5269005, 5269006, 5269007]:
        sources.append(project_root / url_to_relpath(f"{LUNARIS_ORIGIN}/data/leylinechallenge/{lid}.json"))
    for ver in versions:
        for lang in DEFAULT_LANGS:
            base = project_root / "api" / "data" / ver / lang / "char"
            if base.exists():
                sources.extend(base.glob("*.json"))

    return sources


async def _derive_and_fetch_assets(dl: Downloader, project_root: Path) -> None:
    """Phase 3-4: JSON 语义推导资源 URL + JS/CSS 闭包扫描。"""
    # Phase 3: JSON 语义推导
    json_sources = _gather_json_sources(project_root)
    asset_urls = collect_json_semantic_urls(json_sources, include_well_known=True)
    asset_list = sorted(asset_urls)
    for i in range(0, len(asset_list), 600):
        await asyncio.gather(*(dl.fetch(u) for u in asset_list[i:i + 600]))

    # Phase 4: 从 JS/CSS/HTML 中提取绝对 URL
    closure: set[str] = set()
    index_rel = url_to_relpath(f"{LUNARIS_ORIGIN}/")
    index_path = project_root / index_rel
    if index_path.exists():
        try:
            txt = index_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            txt = ""
        closure |= extract_urls_from_text(txt)

    assets_dir = project_root / "site" / "assets"
    if assets_dir.exists():
        for f in assets_dir.iterdir():
            if f.suffix.lower() in (".js", ".css") and f.is_file():
                try:
                    txt = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                closure |= extract_urls_from_text(txt)

    closure = {
        normalize_url(u) for u in closure
        if is_target_url(u) and _is_file_url(u)
    }
    await asyncio.gather(*(dl.fetch(u) for u in sorted(closure)))


def _is_file_url(url: str) -> bool:
    """判断 URL 是否指向静态文件（非 HTML 页面）。"""
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in (
        ".js", ".css", ".json", ".png", ".webp", ".jpg", ".jpeg",
        ".svg", ".ttf", ".otf", ".ico",
    ))


async def run(project_root: Path, concurrency: int = 12) -> None:
    """执行完整的 HTTP 爬取备份流水线。"""
    manifest_path = project_root / "manifest.jsonl"
    manifest = JsonlWriter(manifest_path)
    dl = Downloader(project_root=project_root, manifest=manifest, concurrency=concurrency)

    try:
        await _fetch_seeds(dl)
        await _discover_frontend(dl, project_root)
        await _fetch_api_data(dl, project_root)
        await _derive_and_fetch_assets(dl, project_root)
    finally:
        await dl.close()
        await manifest.close()