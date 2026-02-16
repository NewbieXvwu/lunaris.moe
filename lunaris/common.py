"""共享常量、数据类型和工具函数。"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

__all__ = [
    # 常量
    "LUNARIS_ORIGIN", "API_ORIGIN", "ALLOW_HOSTS",
    "DEFAULT_LANGS", "DEFAULT_CONCURRENCY", "DEFAULT_TIMEOUT_S", "UA",
    # URL / 路径
    "normalize_url", "is_target_url", "should_skip_url",
    "url_to_relpath", "local_file_exists",
    # 文本 / JSON
    "read_json", "safe_read_text", "extract_urls_from_text",
    # 资源推导
    "gather_icon_values", "gather_sprite_presets",
    "gather_special_monster_icons", "stat_icon_from_key",
    "derive_asset_urls_from_icon", "collect_json_semantic_urls",
    # Playwright
    "PageNetState", "wait_network_quiet",
    # IO
    "FetchResult", "JsonlWriter", "LineSet", "Downloader",
    "iter_manifest", "load_existing_urls",
    # 路由
    "build_all_routes",
    # 杂项
    "now_ts", "safe_filename",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

LUNARIS_ORIGIN = "https://lunaris.moe"
API_ORIGIN = "https://api.lunaris.moe"
ALLOW_HOSTS = {"lunaris.moe", "api.lunaris.moe"}
DEFAULT_LANGS = ["en", "chs", "ru", "pt"]
DEFAULT_CONCURRENCY = 12
DEFAULT_TIMEOUT_S = 60.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

_INVALID_WIN_CHARS = str.maketrans({
    "<": "_", ">": "_", ":": "_", '"': "_",
    "/": "_", "\\": "_", "|": "_", "?": "_", "*": "_",
})

# 固定的已知资源 URL（logo、卡片图等）
_WELL_KNOWN_ASSET_URLS = {
    f"{API_ORIGIN}/data/assets/icons/logo.webp",
    f"{API_ORIGIN}/data/assets/icons/logo.png",
    f"{API_ORIGIN}/data/assets/icons/tower-card.png",
    f"{API_ORIGIN}/data/assets/icons/roleplay-card.png",
    f"{API_ORIGIN}/data/assets/icons/leyline-card.png",
}

# 通用图标名称（元素、武器类型、基础属性）
_COMMON_ICON_NAMES = (
    ["pyro", "hydro", "anemo", "electro", "dendro", "cryo", "geo", "physical"]
    + ["sword", "claymore", "polearm", "bow", "catalyst"]
    + ["hp_base", "atk_base", "def_base"]
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def safe_filename(name: str) -> str:
    """将文件名中的非法字符替换为下划线。"""
    return name.translate(_INVALID_WIN_CHARS)


def normalize_url(url: str) -> str:
    """去除 fragment 并统一为 https。"""
    url = urldefrag(url).url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def is_target_url(url: str) -> bool:
    """判断 URL 是否属于 lunaris.moe 或 api.lunaris.moe。"""
    try:
        s = urlsplit(url)
    except Exception:
        return False
    return s.scheme in {"http", "https"} and s.netloc in ALLOW_HOSTS


def should_skip_url(url: str) -> bool:
    """判断 URL 是否应跳过（非目标域名或 CDN 内部路径）。"""
    try:
        s = urlsplit(url)
    except Exception:
        return True
    if s.netloc not in ALLOW_HOSTS:
        return True
    if s.path.startswith("/cdn-cgi/"):
        return True
    return False


def read_json(path: Path) -> Any:
    """读取并解析 JSON 文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def safe_read_text(path: Path) -> str:
    """安全读取文本文件，失败时返回空字符串。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def now_ts() -> str:
    """返回当前时间的 ISO 8601 字符串。"""
    return datetime.now().isoformat(timespec="seconds")


def url_to_relpath(url: str) -> Path:
    """将 URL 映射到本地相对路径（site/ 或 api/）。"""
    s = urlsplit(url)
    path = s.path
    if not path or path.endswith("/"):
        path = (path.rstrip("/") + "/index.html") if path else "/index.html"

    if s.netloc == "api.lunaris.moe":
        rel = Path("api") / path.lstrip("/")
    else:
        rel = Path("site") / path.lstrip("/")

    if s.query:
        qh = hashlib.sha1(s.query.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        rel = rel.with_name(rel.name + f"__q_{qh}")

    parts = [safe_filename(p) for p in rel.parts]
    return Path(*parts)


def local_file_exists(project_root: Path, url: str) -> bool:
    """检查 URL 对应的本地文件是否存在且非空。"""
    p = project_root / url_to_relpath(url)
    return p.exists() and p.is_file() and p.stat().st_size > 0


def extract_urls_from_text(text: str) -> set[str]:
    """从文本中提取 lunaris.moe 相关的 URL。"""
    out: set[str] = set()
    for m in re.finditer(r"https://(?:api\.)?lunaris\.moe/[^\s\"'<>]+", text):
        u = m.group(0).rstrip(",);\\\"]}")
        out.add(normalize_url(u))
    for m in re.finditer(r"(?P<p>/(?:assets|fonts|data)/[A-Za-z0-9_\\-./]+)", text):
        p = m.group("p")
        out.add(normalize_url(urljoin(LUNARIS_ORIGIN, p)))
    return out


# ---------------------------------------------------------------------------
# JSON 语义资源推导
# ---------------------------------------------------------------------------


def gather_icon_values(obj: Any) -> list[str]:
    """递归收集 JSON 中所有 "icon" 字段的值。"""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "icon" and isinstance(v, str) and v:
                out.append(v)
            else:
                out.extend(gather_icon_values(v))
    elif isinstance(obj, list):
        for it in obj:
            out.extend(gather_icon_values(it))
    return out


def gather_sprite_presets(obj: Any) -> set[str]:
    """递归收集 JSON 中 SPRITE_PRESET 引用的编号。"""
    out: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= gather_sprite_presets(v)
    elif isinstance(obj, list):
        for it in obj:
            out |= gather_sprite_presets(it)
    elif isinstance(obj, str):
        for m in re.finditer(r"\{SPRITE_PRESET#(\d+)\}", obj):
            out.add(m.group(1))
    return out


def gather_special_monster_icons(obj: Any) -> set[str]:
    """递归收集 JSON 中 "specialMonsterIcon" 字段的值。"""
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "specialMonsterIcon" and isinstance(v, str) and v:
                out.add(v)
            else:
                out |= gather_special_monster_icons(v)
    elif isinstance(obj, list):
        for it in obj:
            out |= gather_special_monster_icons(it)
    return out


def stat_icon_from_key(key: str) -> str:
    """将 ascensionStats 键名映射为图标文件名。"""
    c = key.lower()
    if "mastery" in c:
        return "em"
    if "recharge" in c:
        return "er"
    if "heal" in c:
        return "healing"
    if "physical" in c:
        return "physical"
    if "crit" in c and "rate" in c:
        return "critrate"
    if "crit" in c and "dmg" in c:
        return "critdmg"
    return re.sub(r"[^a-z]", "", c)


def derive_asset_urls_from_icon(icon: str) -> set[str]:
    """根据 icon 名称推导对应的资源下载 URL。"""
    out: set[str] = set()
    if not icon:
        return out
    if icon.startswith("UI_ItemIcon_"):
        out.add(f"{API_ORIGIN}/data/assets/items/{icon}.webp")
    elif icon.startswith("UI_RelicIcon_"):
        out.add(f"{API_ORIGIN}/data/assets/artifacts/{icon}.webp")
        out.add(f"{API_ORIGIN}/data/assets/artifacts/{icon}.png")
    elif icon.startswith("UI_EquipIcon_"):
        out.add(f"{API_ORIGIN}/data/assets/weaponicon/{icon}.webp")
        gacha = icon.replace("UI_EquipIcon", "UI_Gacha_EquipIcon")
        out.add(f"{API_ORIGIN}/data/assets/weapongacha/{gacha}.webp")
    elif icon.startswith("UI_AvatarIcon_"):
        out.add(f"{API_ORIGIN}/data/assets/avataricon/{icon}.webp")
    elif icon.startswith("UI_CoopImg_"):
        out.add(f"{API_ORIGIN}/data/assets/coopimg/{icon}.webp")
        out.add(f"{API_ORIGIN}/data/assets/avataricon/{icon.replace('UI_CoopImg', 'UI_AvatarIcon')}.webp")
    elif icon.startswith("Skill_") or icon.startswith("UI_Talent_"):
        out.add(f"{API_ORIGIN}/data/assets/skills/{icon}.webp")
    elif icon.startswith("UI_MonsterIcon_"):
        conv = icon.replace("UI_MonsterIcon_", "UI_Img_LeyLineChallenge_")
        out.add(f"{API_ORIGIN}/data/assets/leyline/{conv}.png")
    elif icon.startswith("UI_Img_LeyLineChallenge_"):
        out.add(f"{API_ORIGIN}/data/assets/leyline/{icon}.png")
    return out


def collect_json_semantic_urls(
    json_sources: Iterable[Path],
    *,
    include_well_known: bool = True,
) -> set[str]:
    """从 JSON 文件中通过语义规则推导资源 URL。

    遍历给定的 JSON 文件，收集 icon、sprite preset、monster icon 等字段，
    推导出对应的资源下载 URL。
    """
    icon_values: set[str] = set()
    sprite_numbers: set[str] = set()
    monster_icons: set[str] = set()
    derived_stats: set[str] = set()
    text_urls: set[str] = set()

    for p in json_sources:
        if not p.exists() or not p.is_file() or p.suffix.lower() != ".json":
            continue
        text = safe_read_text(p)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue

        for u in extract_urls_from_text(text):
            if is_target_url(u) and not should_skip_url(u):
                text_urls.add(u)

        for ic in gather_icon_values(data):
            icon_values.add(ic)
        sprite_numbers |= gather_sprite_presets(data)
        monster_icons |= gather_special_monster_icons(data)

        if isinstance(data, dict):
            for v in data.values():
                if (
                    isinstance(v, dict)
                    and "ascensionStats" in v
                    and isinstance(v["ascensionStats"], dict)
                ):
                    for k in v["ascensionStats"]:
                        if isinstance(k, str):
                            derived_stats.add(stat_icon_from_key(k))
            icons_map = data.get("icons")
            if isinstance(icons_map, dict):
                for v in icons_map.values():
                    if isinstance(v, str) and v:
                        icon_values.add(v)

    # 汇总推导
    out: set[str] = set(text_urls)
    for ic in icon_values:
        out |= derive_asset_urls_from_icon(ic)
    for mi in monster_icons:
        if mi.startswith("UI_MonsterIcon_"):
            conv = mi.replace("UI_MonsterIcon_", "UI_Img_LeyLineChallenge_")
            out.add(f"{API_ORIGIN}/data/assets/leyline/{conv}.png")
        elif mi.startswith("UI_Img_LeyLineChallenge_"):
            out.add(f"{API_ORIGIN}/data/assets/leyline/{mi}.png")
    for n in sprite_numbers:
        out.add(f"{API_ORIGIN}/data/assets/icons/{n}.png")
    for name in _COMMON_ICON_NAMES + sorted(derived_stats):
        if name:
            out.add(f"{API_ORIGIN}/data/assets/icons/{name}.webp")
    if include_well_known:
        out |= _WELL_KNOWN_ASSET_URLS
    return {normalize_url(u) for u in out if is_target_url(u) and not should_skip_url(u)}


# ---------------------------------------------------------------------------
# Playwright 共享
# ---------------------------------------------------------------------------


@dataclass
class PageNetState:
    """Playwright 页面网络状态追踪。"""

    inflight: int = 0
    last_event_mono: float = 0.0
    current_route: str = ""
    current_url: str = ""

    def touch(self) -> None:
        """更新最后事件时间戳。"""
        self.last_event_mono = time.monotonic()

    def inc(self) -> None:
        """记录一个新的进行中请求。"""
        self.inflight += 1
        self.touch()

    def dec(self) -> None:
        """记录一个请求完成。"""
        self.inflight = max(0, self.inflight - 1)
        self.touch()


async def wait_network_quiet(
    page: Any,
    state: PageNetState,
    *,
    timeout_ms: int = 4500,
    window_ms: int = 400,
) -> None:
    """等待页面网络活动平静（无进行中请求且持续 *window_ms* 毫秒）。"""
    start = time.monotonic()
    while True:
        now = time.monotonic()
        idle_ms = (now - state.last_event_mono) * 1000
        if state.inflight <= 0 and idle_ms >= window_ms:
            return
        if (now - start) * 1000 >= timeout_ms:
            return
        await page.wait_for_timeout(100)


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """单次下载的结果记录。"""

    url: str
    ok: bool
    status_code: int | None
    path: str
    content_type: str | None
    content_length: int | None
    sha256: str | None
    etag: str | None
    last_modified: str | None
    error: str | None
    note: str | None = None


# ---------------------------------------------------------------------------
# IO 工具类
# ---------------------------------------------------------------------------


class JsonlWriter:
    """追加写入 JSONL 文件，协程安全。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("a", encoding="utf-8", newline="\n")
        self._lock = asyncio.Lock()

    async def write(self, obj: dict[str, Any] | FetchResult) -> None:
        """写入一条记录。"""
        if isinstance(obj, FetchResult):
            obj = dataclasses.asdict(obj)
        line = json.dumps(obj, ensure_ascii=False)
        async with self._lock:
            self._fp.write(line + "\n")
            self._fp.flush()

    async def close(self) -> None:
        """关闭文件。"""
        async with self._lock:
            self._fp.close()


class LineSet:
    """行集合文件，支持断点续跑。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def load(self) -> set[str]:
        """加载已有行集合。"""
        if not self.path.exists():
            return set()
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {ln.strip() for ln in lines if ln.strip()}

    async def add(self, line: str) -> None:
        """追加一行。"""
        line = line.strip()
        if not line:
            return
        async with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------


class Downloader:
    """异步 HTTP 下载器，带重试、去重和 manifest 记录。"""

    def __init__(
        self,
        project_root: Path,
        manifest: JsonlWriter,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.project_root = project_root
        self.manifest = manifest
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "*/*"},
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_s),
        )
        self._seen: set[str] = set()
        self._seen_lock = asyncio.Lock()

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self.client.aclose()

    async def mark_seen(self, url: str) -> bool:
        """标记 URL 为已见，返回是否为首次。"""
        async with self._seen_lock:
            if url in self._seen:
                return False
            self._seen.add(url)
            return True

    async def fetch(self, url: str, *, force: bool = False) -> FetchResult:
        """下载单个 URL 到本地，已存在则跳过。"""
        rel = url_to_relpath(url)
        dest = self.project_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and dest.stat().st_size > 0 and not force:
            rec = FetchResult(
                url=url, ok=True, status_code=200,
                path=str(rel).replace("\\", "/"),
                content_type=None, content_length=dest.stat().st_size,
                sha256=None, etag=None, last_modified=None,
                error="skipped_existing",
            )
            await self.manifest.write(rec)
            return rec

        tmp = dest.with_suffix(dest.suffix + ".part")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

        async with self.sem:
            last_err: str | None = None
            for attempt in range(1, 7):
                try:
                    rec = await self._do_fetch(url, rel, dest, tmp, attempt)
                    if rec is not None:
                        return rec
                except Exception as e:
                    last_err = f"exception:{type(e).__name__}:{e}"
                    await asyncio.sleep(min(30, 2 ** attempt))

            rec = FetchResult(
                url=url, ok=False, status_code=None,
                path=str(rel).replace("\\", "/"),
                content_type=None, content_length=None,
                sha256=None, etag=None, last_modified=None,
                error=last_err or "unknown_error",
            )
            await self.manifest.write(rec)
            return rec

    async def _do_fetch(
        self, url: str, rel: Path, dest: Path, tmp: Path, attempt: int,
    ) -> FetchResult | None:
        """执行单次 HTTP 请求，返回 FetchResult 或 None（需重试）。"""
        async with self.client.stream("GET", url) as resp:
            if resp.status_code in (429, 500, 502, 503, 504):
                await resp.aclose()
                ra = resp.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else min(60, 2 ** attempt)
                await asyncio.sleep(delay)
                return None  # 需要重试

            ct = resp.headers.get("Content-Type")
            etag = resp.headers.get("ETag")
            lm = resp.headers.get("Last-Modified")

            if resp.status_code != 200:
                rec = FetchResult(
                    url=url, ok=False, status_code=resp.status_code,
                    path=str(rel).replace("\\", "/"),
                    content_type=ct,
                    content_length=int(resp.headers.get("Content-Length") or 0) or None,
                    sha256=None, etag=etag, last_modified=lm,
                    error=f"http_{resp.status_code}",
                )
                await self.manifest.write(rec)
                return rec

            h = hashlib.sha256()
            total = 0
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    total += len(chunk)
            tmp.replace(dest)

            rec = FetchResult(
                url=url, ok=True, status_code=200,
                path=str(rel).replace("\\", "/"),
                content_type=ct, content_length=total,
                sha256=h.hexdigest(), etag=etag, last_modified=lm,
                error=None,
            )
            await self.manifest.write(rec)
            return rec


# ---------------------------------------------------------------------------
# manifest 读取
# ---------------------------------------------------------------------------


def iter_manifest(path: Path) -> Iterable[dict[str, Any]]:
    """逐行读取 JSONL manifest 文件。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def load_existing_urls(*manifest_paths: Path) -> set[str]:
    """从 manifest 文件中加载所有已记录的 URL。"""
    out: set[str] = set()
    for mp in manifest_paths:
        for obj in iter_manifest(mp):
            u = obj.get("url")
            if isinstance(u, str):
                out.add(normalize_url(u))
    return out


# ---------------------------------------------------------------------------
# 路由生成
# ---------------------------------------------------------------------------


def build_all_routes(project_root: Path) -> list[str]:
    """根据 API latest 列表生成全站路由。"""
    api_latest = project_root / "api" / "data" / "latest"
    if not api_latest.exists():
        raise RuntimeError(f"未找到本地 latest 列表：{api_latest}")

    charlist = read_json(api_latest / "charlist.json")
    weaponlist = read_json(api_latest / "weaponlist.json")
    artifactlist = read_json(api_latest / "artifactlist.json")
    materiallist = read_json(api_latest / "materiallist.json")

    routes: list[str] = [
        "/", "/characters", "/weapons", "/artifacts", "/materials",
        "/banners", "/endgame", "/leyline",
        "/archive/weapons", "/archive/artifacts", "/archive/materials",
        "/archive/banners", "/archive/endgame",
    ]

    def _sort_key(x: str) -> Any:
        return int(x) if x.isdigit() else x

    for cid in sorted(charlist.keys(), key=_sort_key):
        routes.append(f"/character/{cid}")
    for wid in sorted(weaponlist.keys(), key=_sort_key):
        routes.append(f"/weapon/{wid}")
    for aid in sorted(artifactlist.keys(), key=_sort_key):
        routes.append(f"/artifact/{aid}")
    for mid in sorted(materiallist.keys(), key=_sort_key):
        routes.append(f"/material/{mid}")

    seen: set[str] = set()
    uniq: list[str] = []
    for r in routes:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq
