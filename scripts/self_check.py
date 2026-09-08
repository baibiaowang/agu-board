"""项目离线自检：不访问外网，只验证关键模块、依赖、数据格式和接口兼容关系。"""
import json
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "mainboard_tool"))

REQUIRED = [
    ROOT / "scripts" / "eastmoney_fetch.py",
    ROOT / "scripts" / "cninfo_fetch.py",
    ROOT / "scripts" / "gen_dashboard.py",
    ROOT / "scripts" / "gen_dashboard_incremental.py",
    ROOT / "scripts" / "board_db.py",
    ROOT / "mainboard_tool" / "extract_auto.py",
    ROOT / "mainboard_tool" / "rule_summarize.py",
    ROOT / "mainboard_tool" / "run_full.py",
    ROOT / "mainboard_tool" / "server.py",
]


def check_files():
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED if not p.exists()]
    if missing:
        raise RuntimeError("缺少关键文件: " + ", ".join(missing))


def check_compile():
    for p in REQUIRED:
        py_compile.compile(str(p), doraise=True)


def check_imports():
    import eastmoney_fetch
    import extract_auto
    import rule_summarize
    import run_full
    import board_db
    import gen_dashboard_incremental
    assert callable(eastmoney_fetch.run_fetch)
    assert hasattr(eastmoney_fetch, "RANGE_PRESETS")
    assert callable(extract_auto.main)
    assert callable(rule_summarize.main)
    assert callable(run_full.run)
    assert callable(board_db.connect)
    assert callable(gen_dashboard_incremental.main)


def check_local_data():
    p = ROOT / "cninfo_announce_filtered.json"
    if not p.exists():
        print("[CHECK] 无当前公告缓存，跳过JSON结构检查")
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("cninfo_announce_filtered.json 顶层必须是数组")
    required = {"code", "name", "title", "time", "url", "board", "is_st", "cats"}
    bad = []
    for i, row in enumerate(data[:1000]):
        if not isinstance(row, dict) or not required.issubset(row):
            bad.append(i)
    if bad:
        raise RuntimeError(f"公告缓存结构异常，前1000条中问题行: {bad[:10]}")
    print(f"[CHECK] 当前公告缓存 {len(data)} 条，结构正常")


def check_database():
    db = ROOT / "board.db"
    if not db.exists():
        print("[CHECK] 未安装历史 board.db，等待一次性导入")
        return
    from board_db import connect, integrity_check, seed_stats
    con = connect()
    try:
        integrity_check(con)
        stats = seed_stats(con)
        if stats["announcements"] <= 0 or stats["klines"] <= 0:
            raise RuntimeError("board.db 已存在但历史公告/K线为空")
        print(f"[CHECK] SQLite 正常：公告 {stats['announcements']:,}，K线 {stats['klines']:,}，股票 {stats['stocks']:,}")
    finally:
        con.close()


def main():
    print("=== agu-board self check ===")
    check_files(); print("[OK] 关键文件")
    check_compile(); print("[OK] Python语法编译")
    check_imports(); print("[OK] 模块导入")
    check_local_data()
    check_database()
    print("[OK] 自检完成（未执行外网请求）")


if __name__ == "__main__":
    main()
