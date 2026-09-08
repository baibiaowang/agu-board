"""Incremental dashboard runner backed by the supplied SQLite history database.

It reuses stored K-lines and market values. Network requests happen only for
stocks whose cached K-lines are missing or stale, and new K-lines are persisted.
When REQUIRE_SQLITE=1, absence of the historical seed is a fast, explicit
failure rather than silently falling back to the old network-heavy generator.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from board_db import connect, integrity_check, latest_kline_date, kline_payload, market_cap, persist_to_site, sync_announcement_json, seed_stats
import gen_dashboard


def last_trading_day() -> str:
    d = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


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
        counters = {"kline_cache": 0, "kline_network": 0, "kline_network_new": 0, "kline_write": 0, "mv_cache": 0, "mv_network": 0}

        def add_counter(name: str, value: int = 1) -> None:
            with counter_lock:
                counters[name] += value

        def persist_kline(code: str, payload: dict) -> int:
            from board_db import upsert_kline_payload
            with db_lock:
                written = upsert_kline_payload(con, code, payload)
                con.commit()
            return written

        def cached_fetch_kline(code: str, lmt: int = 120):
            with db_lock:
                latest = latest_kline_date(con, code)
                if latest and latest >= target_day:
                    add_counter("kline_cache")
                    return kline_payload(con, code, lmt)
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
            with db_lock:
                value = market_cap(con, code)
                if value > 0:
                    add_counter("mv_cache")
                    return value
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
            f" K线缓存命中={counters['kline_cache']}, 网络更新={counters['kline_network']}"
            f"（其中新股票={counters['kline_network_new']}）, K线写入={counters['kline_write']};"
            f" 市值缓存命中={counters['mv_cache']}, 市值网络请求={counters['mv_network']}"
        )
        if gen_dashboard.is_github_actions():
            persist_to_site(con, ROOT / "_site" / "data_archive")
    finally:
        con.close()


if __name__ == "__main__":
    main()
