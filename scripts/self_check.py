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
    assert callable(eastmoney_fetch.run_fetch)
    assert hasattr(eastmoney_fetch, "RANGE_PRESETS")
    assert callable(extract_auto.main)
    assert callable(rule_summarize.main)
    assert callable(run_full.run)


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


def main():
    print("=== agu-board self check ===")
    check_files(); print("[OK] 关键文件")
    check_compile(); print("[OK] Python语法编译")
    check_imports(); print("[OK] 模块导入")
    check_local_data()
    print("[OK] 自检完成（未执行外网请求）")


if __name__ == "__main__":
    main()
