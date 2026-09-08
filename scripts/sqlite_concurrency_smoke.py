"""SQLite cross-thread regression test used by CI smoke tests.

The incremental dashboard intentionally shares one SQLite connection with its
worker threads and serializes every DB operation. This test proves that the
Python thread-affinity guard is disabled and that the same locked access pattern
can safely read the historical cache from concurrent workers.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from board_db import connect, kline_payload, latest_kline_date, seed_stats


def main() -> None:
    con = connect()
    lock = threading.RLock()
    try:
        with lock:
            stats = seed_stats(con)
            row = con.execute("SELECT code FROM klines ORDER BY date DESC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("历史K线为空，无法执行跨线程回归测试")
        code = str(row[0])

        def read_once(_: int):
            with lock:
                latest = latest_kline_date(con, code)
                payload = kline_payload(con, code, 5)
                assert latest, f"{code} 没有最新K线日期"
                assert len((payload.get("data") or {}).get("klines") or []) > 0, f"{code} K线缓存为空"
            return latest

        with ThreadPoolExecutor(max_workers=8) as ex:
            dates = list(ex.map(read_once, range(32)))
        if len(dates) != 32 or any(d != dates[0] for d in dates):
            raise RuntimeError("并发读取得到不一致的K线日期")
        print(
            "[SMOKE] SQLite cross-thread access OK: "
            f"stocks={stats['stocks']:,}, announcements={stats['announcements']:,}, "
            f"klines={stats['klines']:,}, sample={code}, latest={dates[0]}"
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
