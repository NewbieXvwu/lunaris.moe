#!/usr/bin/env python3
"""lunaris.moe 离线镜像工具。

用法:
    python lunaris.py backup          一键执行完整备份流水线
    python lunaris.py precompress     预压缩文本文件生成 .gz 旁文件
    python lunaris.py serve           启动本地离线验证服务器
    python lunaris.py sha256          生成 SHA256SUMS.tsv
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def get_project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent


# ── backup ────────────────────────────────────────────────────────────────

async def cmd_backup(args: argparse.Namespace) -> int:
    """执行完整备份流水线（5 个阶段）。"""
    root = get_project_root()
    concurrency = args.concurrency

    print("=" * 50)
    print("Phase 1/5: 主备份 — HTTP 爬取")
    print("=" * 50)
    from lunaris.backup import run as backup_run
    await backup_run(root, concurrency=concurrency)

    if not args.skip_browser:
        print("\n" + "=" * 50)
        print("Phase 2/5: 动态回放 — Playwright 捕获运行时请求")
        print("=" * 50)
        from lunaris.replay import run as replay_run
        await replay_run(root, concurrency=concurrency)

        print("\n" + "=" * 50)
        print("Phase 3/5: 闭包验证 — 从已下载文件提取 URL 补抓")
        print("=" * 50)
        from lunaris.closure import run as closure_run
        await closure_run(root, concurrency=concurrency)

        print("\n" + "=" * 50)
        print("Phase 4/5: 全站扫描 — Playwright 逐页检查")
        print("=" * 50)
        from lunaris.scan import run as scan_run
        await scan_run(root, concurrency=concurrency)

        print("\n" + "=" * 50)
        print("Phase 5/5: UI 交互审计 — 点击 tab/展开折叠")
        print("=" * 50)
        from lunaris.audit import run as audit_run
        await audit_run(root, port=args.port, concurrency=concurrency)

    print("\n备份完成。")
    return 0


# ── serve ─────────────────────────────────────────────────────────────────

def cmd_serve(args: argparse.Namespace) -> int:
    """启动本地离线验证服务器。"""
    root = get_project_root()
    from lunaris.server import load_expected_404, serve

    expected = load_expected_404(root / "logs" / "scan" / "remaining_unavailable.txt")
    expected |= load_expected_404(root / "logs" / "audit" / "expected_upstream_404.txt")

    serve(
        project_root=root,
        port=args.port,
        bind=args.bind,
        rewrite=args.rewrite,
        expected_404=expected,
        workers=getattr(args, "workers", None),
        backlog=getattr(args, "backlog", 512),
        access_log=getattr(args, "access_log", None),
    )
    return 0


# ── precompress ───────────────────────────────────────────────────────────

_COMPRESSIBLE = {".js", ".css", ".html", ".json", ".svg", ".txt", ".xml"}


def cmd_precompress(args: argparse.Namespace) -> int:
    """预压缩 site/ 和 api/ 下的可压缩文件，生成 .gz 旁文件。"""
    root = get_project_root()
    workers = args.workers

    print("=" * 50)
    print("预压缩 .gz 旁文件")
    print("=" * 50)

    # 收集所有可压缩文件
    candidates: list[Path] = []
    for subdir in ("site", "api"):
        base = root / subdir
        if not base.exists():
            continue
        print(f"  扫描 {base} ...")
        for ext in _COMPRESSIBLE:
            for fp in base.rglob(f"*{ext}"):
                if fp.suffix.lower() == ext and fp.is_file():
                    candidates.append(fp)

    skipped_small = 0
    skipped_fresh = 0
    to_compress: list[Path] = []

    for fp in candidates:
        try:
            st = fp.stat()
        except OSError:
            continue
        if st.st_size <= 256:
            skipped_small += 1
            continue
        gz = fp.with_suffix(fp.suffix + ".gz")
        try:
            gz_st = gz.stat()
            if gz_st.st_mtime >= st.st_mtime:
                skipped_fresh += 1
                continue
        except OSError:
            pass
        to_compress.append(fp)

    print(f"  可压缩文件: {len(candidates)} 个")
    print(f"  跳过 (<=256B): {skipped_small} 个")
    print(f"  跳过 (已最新): {skipped_fresh} 个")
    print(f"  待压缩: {len(to_compress)} 个")
    print(f"  并发数: {workers}")
    print("=" * 50)

    if not to_compress:
        print("  无需压缩。")
        return 0

    compressed = 0
    t0 = time.time()

    def _compress_one(fp: Path) -> Path:
        gz = fp.with_suffix(fp.suffix + ".gz")
        with open(fp, "rb") as f_in:
            raw = f_in.read()
        with open(gz, "wb") as f_out:
            with gzip.GzipFile(fileobj=f_out, mode="wb", compresslevel=9) as gz_f:
                gz_f.write(raw)
        return fp

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_compress_one, fp): fp for fp in to_compress}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                compressed += 1
                if i % 200 == 0 or i == len(to_compress):
                    rel = result.relative_to(root)
                    print(f"  [{i}/{len(to_compress)}] {rel}")
            except Exception as e:
                print(f"  Error: {futures[future]}: {e}")

    elapsed = time.time() - t0
    print("=" * 50)
    print(f"  已压缩: {compressed} 个")
    print(f"  完成! 耗时 {elapsed:.1f} 秒")
    print("=" * 50)
    return 0


# ── sha256 ────────────────────────────────────────────────────────────────

def cmd_sha256(args: argparse.Namespace) -> int:
    """生成 SHA256SUMS.tsv 校验文件。"""
    root = get_project_root()
    sha_file = root / "SHA256SUMS.tsv"
    records = []
    for subdir in ("site", "api"):
        base = root / subdir
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.name.endswith(".part"):
                continue
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            rel = p.relative_to(root).as_posix()
            records.append((rel, p.stat().st_size, h.hexdigest()))

    with sha_file.open("w", encoding="utf-8", newline="\n") as f:
        f.write("path\tsize\tsha256\n")
        for rel, size, digest in records:
            f.write(f"{rel}\t{size}\t{digest}\n")

    print(f"已生成 {sha_file.name}，共 {len(records)} 个文件")
    return 0


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="lunaris.moe 离线镜像工具")
    sub = parser.add_subparsers(dest="command")

    bk = sub.add_parser("backup", help="一键执行完整备份流水线")
    bk.add_argument("--concurrency", type=int, default=12, help="下载并发数（默认 12）")
    bk.add_argument("--port", type=int, default=9001, help="UI 审计用的本地服务器端口")
    bk.add_argument("--skip-browser", action="store_true", help="跳过 Playwright 阶段（仅 HTTP 爬取）")

    sv = sub.add_parser("serve", help="启动本地离线验证服务器")
    sv.add_argument("--port", type=int, default=int(os.environ.get("LUNARIS_SERVE_PORT", "9000")))
    sv.add_argument("--bind", default="0.0.0.0")
    sv.add_argument("--rewrite", action=argparse.BooleanOptionalAction, default=True)
    sv.add_argument("-w", "--workers", type=int,
                    default=min(4, os.cpu_count() or 1),
                    help="ASGI worker 数 (默认 min(4, CPU核心数))")
    sv.add_argument("--backlog", type=int, default=512, help="连接队列大小 (默认 512)")
    sv.add_argument("--access-log", default=None,
                    help="Access log 路径，'-' 为 stdout (默认关闭)")

    pc = sub.add_parser("precompress", help="预压缩 site/ 和 api/ 下的文本文件")
    pc.add_argument("-w", "--workers", type=int, default=10, help="并发数 (默认 10)")

    sub.add_parser("sha256", help="生成 SHA256SUMS.tsv")

    args = parser.parse_args()
    if args.command == "backup":
        return asyncio.run(cmd_backup(args))
    elif args.command == "serve":
        return cmd_serve(args)
    elif args.command == "precompress":
        return cmd_precompress(args)
    elif args.command == "sha256":
        return cmd_sha256(args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
