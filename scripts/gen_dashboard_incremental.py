"""Incremental dashboard runner backed by the supplied SQLite history database.

It monkey-patches the existing dashboard generator so stored K-lines and market
values are reused. Network requests happen only for stocks without current
cached K-lines (or without a market value). Newly fetched K-lines are written
back to board.db and persisted by the workflow.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from board_db import connect, integrity_check, latest_kline_date, kline_payload, market_cap, persist_to_site, sync_announcement_json, seed_stats
import gen_dashboard


def beijing_today() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().strftime("%Y-%m-%d")


def last_trading_day() -> str:
    d = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def main() -> None:
    con = connect()
    try:
        integrity_check(con)
        stats = seed_stats(con)
        print(
            "[DB] 历史基线："
            f" stocks={stats['stocks']:,}, announcements={stats['announcements']:,}, klines={stats['klines']:,}; "
            f"公告日期={stats['ann_min_date']}~{stats['ann_max_date']}, K线日期={stats['kline_min_date']}~{stats['kline_max_date']}"
        )

        json_files = [ROOT / "cninfo_announce_all.json", ROOT / "cninfo_announce_filtered.json"]
        scanned, inserted = sync_announcement_json(con, json_files)
        if scanned:
            print(f"[DB] 增量同步公告：扫描 {scanned:,} 条，新增/更新 {inserted:,} 条")

        original_fetch_kline = gen_dashboard.fetch_kline
        original_fetch_market_cap = gen_dashboard.fetch_market_cap
        target_day = last_trading_day()
        counters = {"kline_cache": 0, "kline_network": 0, "kline_network_new": 0, "kline_write": 0, "kline_no_data": 0, "mv_cache": 0, "mv_network": 0}

        def cached_fetch_kline(code: str, lmt: int = 120):
            latest = latest_kline_date(con, code)
            if latest and latest >= target_day:
                counters["kline_cache"] += 1
                return kline_payload(con, code, lmt)
            counters["kline_network"] += 1
            if not latest:
                counters["kline_network_new"] += 1
            payload = original_fetch_kline(code, lmt)
            written = 0
            try:
                written = persist_kline(code, payload)
            except Exception as exc:
                print(f"  [DB] K线写入失败 {code}: {exc}", flush=True)
            counters["kline_write"] += written
            return payload

        def persist_kline(code: str, payload: dict) -> int:
            from board_db import upsert_kline_payload
            return upsert_kline_payload(con, code, payload)

        def cached_market_cap(code: str):
            value = market_cap(con, code)
            if value > 0:
                counters["mv_cache"] += 1
                return value
            counters["mv_network"] += 1
            value = original_fetch_market_cap(code)
            if value > 0:
                con.execute("UPDATE stocks SET market_value=?, updated_at=? WHERE code=?", (float(value), dt.datetime.now().isoformat(timespec="seconds"), code))
                con.commit()
            return value

        gen_dashboard.fetch_kline = cached_fetch_kline
        gen_dashboard.fetch_market_cap = cached_market_cap
        try:
            gen_dashboard.main()
        finally:
            gen_dashboard.fetch_kline = original_fetch_kline
            gen_dashboard.fetch_market_cap = original_fetch_market_cap

        con.commit()
        print(
            "[DB] 看板增量统计："
            f" K线缓存命中={counters['kline_cache']}, 网络更新={counters['kline_network']}"
            f"（其中新股票={counters['kline_network_new']}）, K线写入={counters['kline_write']};"
            f" 市值缓存命中={counters['mv_cache']}, 市值网络请求={counters['mv_network']}"
        )
        if counters["kline_no_data"]:
            print(f"[DB] 无K线数据={counters['kline_no_data']}")
        if gen_dashboard.is_github_actions():
            persist_to_site(con, ROOT / "_site" / "data_archive")
    finally:
        con.close()


if __name__ == "__main__":
    main()
