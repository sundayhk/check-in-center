#!/usr/bin/env python3
"""signin-hub 统一签到入口。

用法：
    python main.py                    # 跑全部启用的站点
    python main.py --site nodeseek    # 只跑 NodeSeek
    python main.py --force            # 忽略当日已完成记录，强制再跑
    python main.py --dry-run          # 只演练，不发起任何网络请求
    python main.py --test-notify      # 只测通知链路
    python main.py --list-sites       # 列出已注册站点
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.base import Context                      # noqa: E402
from core.config import load_config               # noqa: E402
from core.notify import Notifier                   # noqa: E402
from core.runner import Runner, format_summary     # noqa: E402
from core.state import State                       # noqa: E402
from sites import REGISTRY, build_sites            # noqa: E402

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def ensure_tempdir() -> None:
    """确保临时目录可用。

    tempfile.gettempdir() 找不到可用目录时会静默回退到当前工作目录，
    结果就是项目根目录里散落一堆 8 位随机名的垃圾文件。
    这里显式探测一次，不可用就兜底到 data/tmp。
    """
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(delete=True):
            return
    except Exception:
        pass
    fallback = ROOT / "data" / "tmp"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(fallback)
    except Exception:
        pass


def setup_logging(level: str, log_dir: str) -> logging.Logger:
    handlers: list = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        p = ROOT / log_dir
        p.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(p / "signin.log", encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=LOG_FMT, datefmt="%H:%M:%S", handlers=handlers, force=True,
    )
    return logging.getLogger("signin-hub")


def main() -> int:
    ensure_tempdir()
    ap = argparse.ArgumentParser(description="signin-hub 多站点自动签到")
    ap.add_argument("--config", help="配置文件路径（默认 ./config.yaml）")
    ap.add_argument("--site", action="append", help="只跑指定站点，可重复使用")
    ap.add_argument("--account", help="只跑指定账号名")
    ap.add_argument("--force", action="store_true", help="忽略当日已完成记录")
    ap.add_argument("--dry-run", action="store_true", help="不发起真实请求")
    ap.add_argument("--no-notify", action="store_true", help="不推送通知")
    ap.add_argument("--test-notify", action="store_true", help="只发送一条测试通知")
    ap.add_argument("--list-sites", action="store_true", help="列出已注册站点")
    ap.add_argument("--daemon", action="store_true", help="常驻模式，每隔一段时间跑一轮")
    ap.add_argument("--interval", type=int, default=1800, help="常驻模式的间隔秒数")
    ap.add_argument("--doctor", action="store_true", help="环境体检：依赖/浏览器/配置/通知")
    ap.add_argument("--probe", action="store_true", help="配合 --doctor，额外做网络连通性探测")
    args = ap.parse_args()

    if args.list_sites:
        print("已注册站点：" + "、".join(f"{k}" for k in REGISTRY))
        return 0

    cfg = load_config(args.config)
    log = setup_logging(cfg.runtime.log_level, cfg.runtime.log_dir)

    if args.doctor:
        from core.doctor import run as doctor_run
        source = args.config or (
            "config.yaml" if (ROOT / "config.yaml").exists() else "环境变量模式")
        print("\n".join(doctor_run(cfg, source, probe=args.probe)))
        return 0

    notifier = Notifier(cfg.notify, logger=log)
    if args.test_notify:
        notifier.send("signin-hub 通知测试", "收到这条说明通知配置正常。")
        return 0

    while True:
        code = run_once(cfg, log, args)
        if not args.daemon:
            return code
        log.info("常驻模式：%s 秒后进入下一轮", args.interval)
        time.sleep(max(60, args.interval))


def run_once(cfg, log, args) -> int:
    ctx = Context(runtime=cfg.runtime, logger=log)
    sites = build_sites(cfg, ctx)

    if args.site:
        wanted = set(args.site)
        sites = {k: v for k, v in sites.items() if k in wanted}
    if args.account:
        for s in sites.values():
            s.cfg.accounts = [a for a in s.cfg.accounts if a.name == args.account]

    if not sites:
        log.warning("没有可执行的站点，请检查配置与账号")
        return 1

    state = State(ROOT / cfg.runtime.state_db, logger=log)
    runner = Runner(cfg, state, log)
    log.info("本次任务站点：%s", "、".join(sites.keys()))

    try:
        results = runner.run(sites, force=args.force, dry_run=args.dry_run)
    finally:
        state.close()

    summary = format_summary(results)
    print("\n" + summary)

    if not args.no_notify and results:
        failed = [r for r in results if r.status == "failed"]
        title = "signin-hub 签到汇总" + ("（有失败项）" if failed else "")
        notifier.send(title, summary)

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except Exception as e:  # 顶层兜底，保证 cron 里不会静默失败
        print(f"致命错误：{type(e).__name__}: {e}", file=sys.stderr)
        os._exit(1)
