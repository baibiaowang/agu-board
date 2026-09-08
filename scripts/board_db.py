"""SQLite history/cache backend for agu-board.

Uses the supplied board.db as the historical baseline. In CI the database is
persisted as data_archive/board.db.zst on GitHub Pages, so later runs only need
incremental announcement and K-line updates.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

from path_util import data_root, is_github_actions

BASE = data_root()
DB_PATH = BASE / "board.db"
SEED_DIR = BASE / "data_seed"

REQUIRED_TABLES = {"announcements", "klines", "stocks"}


def _run_checked(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def restore_seed_if_needed() -> bool:
    """Restore board.db from the persistent Pages copy or one-time seed."""
    if DB_PATH.exists() and DB_PATH.stat().st_size > 1024:
        return False

    candidates = [
        BASE / "board.db.zst",
        BASE / "board.db.gz",
        SEED_DIR / "board.db.zst",
        SEED_DIR / "board.db.gz",
    ]
    for src in candidates:
        if not src.exists() or src.stat().st_size <= 1024:
            continue
        tmp = DB_PATH.with_suffix(DB_PATH.suffix + ".tmp")
        try:
            if src.suffix == ".zst":
                _run_checked(["zstd", "-q", "-d", "-f", str(src), "-o", str(tmp)])
            else:
                with gzip.open(src, "rb") as rf, open(tmp, "wb") as wf:
                    shutil.copyfileobj(rf, wf, length=1024 * 1024)
            if tmp.stat().st_size <= 1024:
                raise RuntimeError("恢复后的 board.db 异常过小")
            tmp.replace(DB_PATH)
            print(f"[DB] 已恢复历史数据库：{src}")
            return True
        finally:
            tmp.unlink(missing_ok=True)
    return False


def connect() -> sqlite3.Connection:
    restore_seed_if_needed()
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"找不到历史数据库 {DB_PATH}。请把提供的 board.db.zst 放入 data_seed/，或直接放入数据目录。"
        )
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = REQUIRED_TABLES - tables
    if missing:
        con.close()
        raise RuntimeError(f"board.db 缺少必要数据表：{sorted(missing)}")
    return con


def integrity_check(con: sqlite3.Connection) -> None:
    row = con.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise RuntimeError(f"board.db 完整性检查失败：{row}")


def latest_kline_date(con: sqlite3.Connection, code: str) -> str:
    row = con.execute("SELECT MAX(date) FROM klines WHERE code=?", (code,)).fetchone()
    return str(row[0] or "")


def kline_payload(con: sqlite3.Connection, code: str, limit: int = 120) -> dict:
    rows = con.execute(
        "SELECT date,open,close,high,low,volume,change_pct "
        "FROM klines WHERE code=? ORDER BY date DESC LIMIT ?",
        (code, int(limit)),
    ).fetchall()
    rows.reverse()
    klines = []
    for r in rows:
        # gen_dashboard 只需要前6列，额外字段也兼容保留。
        date_s, op, close, high, low, volume, chg = r
        vals = [date_s, op, close, high, low, volume]
        klines.append(",".join("" if v is None else str(v) for v in vals))
    return {"data": {"klines": klines, "name": code}} if klines else {"data": {"klines": [], "name": code}}


def upsert_kline_payload(con: sqlite3.Connection, code: str, payload: dict) -> int:
    rows = (payload.get("data") or {}).get("klines") or []
    added = 0
    for item in rows:
        parts = str(item).split(",")
        if len(parts) < 6:
            continue
        try:
            d, op, close, high, low, volume = parts[:6]
            con.execute(
                "INSERT INTO klines(code,date,open,high,low,close,volume,change_pct) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code,date) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume",
                (code, d, float(op), float(high), float(low), float(close), float(volume), None),
            )
            added += 1
        except (TypeError, ValueError):
            continue
    return added


def market_cap(con, code: str) -> float:
    row = con.execute("SELECT market_value FROM stocks WHERE code=?", (code,)).fetchone()
    try:
        return float(row[0] or 0) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ann_id(row: dict) -> str:
    art = str(row.get("art_code") or row.get("ann_id") or "").strip()
    if art:
        return art[:64]
    raw = "|".join(str(row.get(k, "")) for k in ("code", "title", "time", "url"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def sync_announcement_json(con: sqlite3.Connection, files: Iterable[Path]) -> tuple[int, int]:
    """Incrementally import normalized announcement JSON into SQLite."""
    seen = 0
    inserted = 0
    for fp in files:
        if not fp.exists() or not fp.is_file():
            continue
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            title = str(row.get("title") or "").strip()
            if not code or not title:
                continue
            ann_id = _ann_id(row)
            time_s = str(row.get("time") or row.get("date") or "")[:19]
            date_s = time_s[:10]
            cats = row.get("cats") or []
            category = str(cats[0] if cats else row.get("category") or "公告")[:32]
            board = str(row.get("board") or "")[:16]
            url = str(row.get("url") or row.get("pdf_url") or "")[:512]
            name = str(row.get("name") or "")[:32]
            con.execute(
                "INSERT INTO stocks(code,name,board,market_value,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE stocks.name END, "
                "board=CASE WHEN excluded.board<>'' THEN excluded.board ELSE stocks.board END, "
                "updated_at=excluded.updated_at",
                (code, name, board, 0.0, dt.datetime.now().isoformat(timespec="seconds")),
            )
            before = con.total_changes
            con.execute(
                "INSERT INTO announcements(ann_id,code,name,title,date,category,board,key_numbers,url,summary,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ann_id) DO UPDATE SET "
                "name=excluded.name, title=excluded.title, date=excluded.date, "
                "category=CASE WHEN excluded.category<>'' THEN excluded.category ELSE announcements.category END, "
                "board=CASE WHEN excluded.board<>'' THEN excluded.board ELSE announcements.board END, "
                "url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE announcements.url END",
                (ann_id, code, name, title, date_s, category, board, str(row.get("key_numbers") or "")[:256], url, "", dt.datetime.now().isoformat(timespec="seconds")),
            )
            if con.total_changes > before:
                inserted += 1
            seen += 1
    con.commit()
    return seen, inserted


def seed_stats(con: sqlite3.Connection) -> dict:
    out = {}
    for table in ("stocks", "announcements", "klines"):
        out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    out["ann_min_date"], out["ann_max_date"] = con.execute("SELECT MIN(date),MAX(date) FROM announcements").fetchone()
    out["kline_min_date"], out["kline_max_date"] = con.execute("SELECT MIN(date),MAX(date) FROM klines").fetchone()
    return out


def persist_to_site(con: sqlite3.Connection, site_archive: Path) -> Path:
    """Checkpoint WAL, then compress the live DB for next CI run."""
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    site_archive.mkdir(parents=True, exist_ok=True)
    out = site_archive / "board.db.zst"
    tmp = out.with_suffix(out.suffix + ".tmp")
    _run_checked(["zstd", "-1", "-q", "-f", str(DB_PATH), "-o", str(tmp)])
    tmp.replace(out)
    print(f"[DB] 已持久化：{out} ({out.stat().st_size:,} bytes)")
    return out
