"""Incremental dashboard runner backed by the supplied SQLite history database.

It reuses stored K-lines and market values. Historical-only stocks use the
SQLite cache without network refresh; only stocks appearing in the current
announcement batch are eligible for K-line network updates. Recent stale
caches are also reused to avoid repeated requests for suspended or delayed
symbols. When REQUIRE_SQLITE=1, absence of the historical seed is a fast,
explicit failure rather than silently falling back to the old network-heavy
generator.

网络刷新预算
------------
"本期公告涉及的股票"会随抓取窗口放大：手动触发一次 1 个月档位、或指定日期区间，
本期股票数可达上千只，而每只股票最坏要经历东财 3 次重试 + 腾讯 3 次重试
（单次 socket 超时 12s，另有退避 sleep），于是这一步会稳定吃满 30 分钟
并被 GitHub Actions 掐掉（历史报错：
"The action 'Generate dashboard data (SQLite incremental)' has timed out after 30 minutes."）。

因此这里引入两道硬闸，任一触发后剩余股票只读 SQLite 缓存（或返回空），
不再发起任何网络请求：

* ``KLINE_REFRESH_BUDGET_SECONDS`` —— 墙钟预算，默认 840s（14 分钟）。
* ``KLINE_MAX_NETWORK`` / ``MARKET_CAP_MAX_NETWORK`` —— 网络请求条数上限，默认各 600。

三个值设为 0 表示关闭对应的网络刷新。看板因此总能在 step 上限内产出，
未刷新到的股票会在后续几轮增量里自然补齐（KLINE_REUSE_STALE_DAYS 内的缓存仍可复用）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from board_db import connect, integrity_check, latest_kline_date, kline_payload, market_cap, persist_to_site, sync_announcement_json, seed_stats
import gen_dashboard

# 看板只渲染最近 60 个交易日（gen_dashboard 里也是按 60 根截断的），
# 多取的 K 线只会放大 DB 读取、序列化和前端体积。
MAX_KLINE_BARS = 60


def last_trading_day() -> str:
    d = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _date_cutoff(days: int) -> str:
    """Return a Beijing-calendar cutoff date used for K-line refresh decisions."""
    d = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() - dt.timedelta(days=max(0, days))
    return d.strftime("%Y-%m-%d")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _current_announcement_codes() -> set[str]:
    """Return stock codes present in the current filtered announcement batch."""
    path = ROOT / "cninfo_announce_filtered.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(rows, list):
        return set()
    return {str(r.get("code") or "").strip() for r in rows if isinstance(r, dict) and str(r.get("code") or "").strip()}


def main() -> None:
    require_sqlite = os.environ.get("REQUIRE_SQLITE", "0") == "1"
    try:
        con = connect()
    except FileNotFoundError:
        if require_sqlite:
            raise RuntimeError(
                "历史 SQLite 基线未安装：请将 board.db.zst 放入 data_seed/，或完成一次 gh-pages 数据库恢复后再运行生产增量看板。"
            )
        print("[DB] 未安装历史 SQLite 基线，暂时使用原看板生成器。")
        gen_dashboard.main()
        return

    # gen_dashboard 使用线程并发请求 K 线/市值。
    # SQLite 连接在 board_db.connect() 中显式关闭 thread-affinity 检查，
    # 这里再用 RLock 串行保护所有 DB 操作；HTTP 请求始终在锁外执行。
    db_lock = threading.RLock()
    counter_lock = threading.Lock()

    try:
        with db_lock:
            integrity_check(con)
            stats = seed_stats(con)
        print(
            "[DB] 历史基线："
            f" stocks={stats['stocks']:,}, announcements={stats['announcements']:,}, klines={stats['klines']:,}; "
            f"公告日期={stats['ann_min_date']}~{stats['ann_max_date']}, K线日期={stats['kline_min_date']}~{stats['kline_max_date']}"
        )

        with db_lock:
            scanned, changed = sync_announcement_json(
                con,
                [ROOT / "cninfo_announce_all.json", ROOT / "cninfo_announce_filtered.json"],
            )
        if scanned:
            print(f"[DB] 增量同步公告：扫描 {scanned:,} 条，新增/更新 {changed:,} 条")

        original_fetch_kline = gen_dashboard.fetch_kline
        original_fetch_market_cap = gen_dashboard.fetch_market_cap
        target_day = last_trading_day()
        kline_reuse_days = _env_int("KLINE_REUSE_STALE_DAYS", 3)
        reuse_cutoff = _date_cutoff(kline_reuse_days)
        current_codes = _current_announcement_codes()

        budget_seconds = _env_int("KLINE_REFRESH_BUDGET_SECONDS", 840)
        net_limit = {
            "kline_network": _env_int("KLINE_MAX_NETWORK", 600),
            "mv_network": _env_int("MARKET_CAP_MAX_NETWORK", 600),
        }
        deadline = time.monotonic() + budget_seconds
        print(
            f"[DB] 本期公告涉及股票：{len(current_codes):,} 只；历史股票不再联网刷新K线"
        )
        print(
            f"[DB] 网络刷新预算：K线≤{net_limit['kline_network']} 只, 市值≤{net_limit['mv_network']} 只, "
            f"总时长≤{budget_seconds}s；超出部分只读缓存"
        )
        counters = {
            "kline_cache": 0,
            "kline_stale_reuse": 0,
            "kline_historical_reuse": 0,
            "kline_network": 0,
            "kline_network_new": 0,
            "kline_write": 0,
            "kline_budget_skip": 0,
            "mv_cache": 0,
            "mv_network": 0,
            "mv_budget_skip": 0,
        }

        def add_counter(name: str, value: int = 1) -> None:
            with counter_lock:
                counters[name] += value

        def allow_network(kind: str) -> bool:
            """预算闸门：超时或超条数后永久关闭网络通道。"""
            if time.monotonic() >= deadline:
                return False
            with counter_lock:
                return counters[kind] < net_limit[kind]

        # 写入按批提交：原实现每只股票 commit 一次，大回填时被提交开销拖垮。
        write_batch = {"pending": 0}

        def persist_kline(code: str, payload: dict) -> int:
            from board_db import upsert_kline_payload
            with db_lock:
                written = upsert_kline_payload(con, code, payload)
                write_batch["pending"] += 1
                if write_batch["pending"] >= 25:
                    con.commit()
                    write_batch["pending"] = 0
            return written

        def cached_fetch_kline(code: str, lmt: int = MAX_KLINE_BARS):
            code = str(code or "").strip()
            # 调用方可能仍按旧的 120 根请求，统一收敛到看板真正需要的条数。
            lmt = max(1, min(int(lmt or MAX_KLINE_BARS), MAX_KLINE_BARS))
            with db_lock:
                latest = latest_kline_date(con, code)
                if latest and latest >= target_day:
                    add_counter("kline_cache")
                    return kline_payload(con, code, lmt)
                if code not in current_codes:
                    # 历史90天股票只负责展示历史关系，不应因本次看板生成而产生网络请求。
                    if latest:
                        add_counter("kline_historical_reuse")
                        return kline_payload(con, code, lmt)
                    return {"data": {"klines": [], "name": code}}
                if latest and latest >= reuse_cutoff:
                    # 数据只比目标日旧几天：对停牌/数据源延迟的股票直接复用。
                    add_counter("kline_stale_reuse")
                    return kline_payload(con, code, lmt)
                if not allow_network("kline_network"):
                    # 预算耗尽：不再联网，用已有缓存兜底，保证整步能在 step 超时前跑完。
                    add_counter("kline_budget_skip")
                    if latest:
                        return kline_payload(con, code, lmt)
                    return {"data": {"klines": [], "name": code}}
                add_counter("kline_network")
                if not latest:
                    add_counter("kline_network_new")

            payload = original_fetch_kline(code, lmt)
            try:
                written = persist_kline(code, payload)
                add_counter("kline_write", written)
            except Exception as exc:
                print(f"  [DB] K线写入失败 {code}: {exc}", flush=True)
            return payload

        def cached_market_cap(code: str):
            code = str(code or "").strip()
            with db_lock:
                value = market_cap(con, code)
                if value > 0:
                    add_counter("mv_cache")
                    return value
                # 只有本期公告涉及的股票才允许补市值；历史展示股票直接返回数据库已有值。
                if code not in current_codes:
                    return 0
                if not allow_network("mv_network"):
                    add_counter("mv_budget_skip")
                    return 0
                add_counter("mv_network")

            value = original_fetch_market_cap(code)
            if value > 0:
                with db_lock:
                    con.execute(
                        "UPDATE stocks SET market_value=?, updated_at=? WHERE code=?",
                        (float(value), dt.datetime.now().isoformat(timespec="seconds"), code),
                    )
                    con.commit()
            return value

        gen_dashboard.fetch_kline = cached_fetch_kline
        gen_dashboard.fetch_market_cap = cached_market_cap
        try:
            gen_dashboard.main()
        finally:
            gen_dashboard.fetch_kline = original_fetch_kline
            gen_dashboard.fetch_market_cap = original_fetch_market_cap

        with db_lock:
            con.commit()
        print(
            "[DB] 看板增量统计："
            f" K线当天缓存命中={counters['kline_cache']}, "
            f"近期缓存复用={counters['kline_stale_reuse']}, "
            f"历史缓存复用={counters['kline_historical_reuse']}, "
            f"网络更新={counters['kline_network']}（其中新股票={counters['kline_network_new']}）, "
            f"预算内跳过={counters['kline_budget_skip']}, "
            f"K线写入={counters['kline_write']};"
            f" 市值缓存命中={counters['mv_cache']}, 市值网络请求={counters['mv_network']}, "
            f"市值预算内跳过={counters['mv_budget_skip']}"
        )
        if counters["kline_budget_skip"] or counters["mv_budget_skip"]:
            print(
                "[DB] 本轮触发了网络刷新预算：部分股票沿用缓存（或留空）。"
                "如需一次刷新更多，请调大 KLINE_REFRESH_BUDGET_SECONDS / KLINE_MAX_NETWORK。",
                flush=True,
            )
        if gen_dashboard.is_github_actions():
            persist_to_site(con, ROOT / "_site" / "data_archive")
    finally:
        con.close()


if __name__ == "__main__":
    main()
