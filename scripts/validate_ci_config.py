"""Static validation for the GitHub Actions contract used by agu-board."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(path: Path, text: str, label: str) -> None:
    content = read(path)
    if text not in content:
        raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} missing {label}: {text!r}")
    print(f"[OK] {path.relative_to(ROOT)}: {label}")


def require_import(path: Path, module: str, name: str, label: str) -> None:
    """检查 path 从 module 导入了 name。

    不能用 `require(path, "from universe import merge_archive", ...)` 做整串匹配：
    一旦同模块多导入几个名字（如 `from universe import display_title, merge_archive,
    norm_title`），整串匹配就会误报失败。这里解析导入名再比对。
    """
    pattern = re.compile(rf"^\s*from\s+{re.escape(module)}\s+import\s+(.+?)\s*$", re.M)
    imported = set()
    for m in pattern.finditer(read(path)):
        imported.update(n.strip() for n in m.group(1).split(",") if n.strip())
    if name not in imported:
        raise SystemExit(
            f"[FAIL] {path.relative_to(ROOT)} missing {label}: 'from {module} import {name}'"
        )
    print(f"[OK] {path.relative_to(ROOT)}: {label}")


def require_any(path: Path, texts: tuple[str, ...], label: str) -> None:
    content = read(path)
    if not any(text in content for text in texts):
        raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} missing {label}: expected one of {texts!r}")
    print(f"[OK] {path.relative_to(ROOT)}: {label}")


def main() -> None:
    production = WF / "update-board.yml"
    manual = WF / "manual-incremental-update.yml"
    smoke = WF / "smoke-test.yml"
    db_backend = ROOT / "scripts" / "board_db.py"
    db_runner = ROOT / "scripts" / "gen_dashboard_incremental.py"
    db_test = ROOT / "scripts" / "sqlite_concurrency_smoke.py"
    index_html = ROOT / "reports" / "dashboard" / "index.html"
    universe_mod = ROOT / "universe.py"
    gen_dashboard = ROOT / "scripts" / "gen_dashboard.py"
    fetch_script = ROOT / "scripts" / "eastmoney_fetch.py"
    rule_script = ROOT / "mainboard_tool" / "rule_summarize.py"

    for path in (production, manual, smoke, db_backend, db_runner, db_test,
                 index_html, universe_mod, gen_dashboard, fetch_script, rule_script):
        if not path.exists():
            raise SystemExit(f"[FAIL] missing required file: {path.relative_to(ROOT)}")

    require(production, "name: Update A-Share Announcement Board", "friendly workflow name")
    require(production, "workflow_dispatch:", "manual dispatch")
    require(production, "timezone: 'Asia/Shanghai'", "explicit Asia/Shanghai schedule timezone")
    require_any(production, ("'source': 'eastmoney'", '"source": "eastmoney"', "source: 'eastmoney'"), "Eastmoney runtime status")
    require(production, "group: announcement-board", "shared concurrency group")
    require(production, "scripts/eastmoney_fetch.py", "Eastmoney fetch path")
    require(production, "mainboard_tool/rule_summarize.py", "rule report path")
    require(production, "scripts/gen_dashboard_incremental.py", "SQLite incremental dashboard path")
    require(production, "board.db.zst", "SQLite persistence artifact")
    require(production, "REQUIRE_SQLITE: '1'", "production SQLite enforcement")
    require(production, "steps.report_window.outputs.start", "report start output wiring")
    require(production, "steps.report_window.outputs.end", "report end output wiring")
    if "git push origin main" in read(production):
        raise SystemExit("[FAIL] production workflow must not self-push to main")
    print("[OK] production workflow does not self-push main")

    require(manual, "name: Manual Incremental Announcement Update", "friendly manual workflow name")
    require(manual, "on:\n  workflow_dispatch:", "manual-only trigger")
    require(manual, "mode': 'incremental", "incremental mode status")
    require(manual, "manual-incremental-update.yml", "self-service dashboard target")
    require(manual, "PRE-DEPLOY HEALTH: OK", "pre-deploy health gate")
    require(manual, "scripts/gen_dashboard_incremental.py", "manual SQLite incremental path")
    require(manual, "REQUIRE_SQLITE: '1'", "manual SQLite enforcement")
    require(manual, "board.db.zst", "manual SQLite persistence artifact")

    require(smoke, "End-to-End Smoke Test", "smoke workflow")
    require(smoke, "scripts/eastmoney_fetch.py", "Eastmoney smoke path")
    require(smoke, "mainboard_tool/extract_auto.py", "content extraction smoke path")
    require(smoke, "scripts/gen_dashboard_incremental.py", "SQLite incremental smoke path")
    require(smoke, "historical SQLite seed", "seed detection")
    require(smoke, "sqlite_concurrency_smoke.py", "SQLite cross-thread regression test")

    # 检查真正的实现配置，而不是依赖注释措辞。
    require(db_backend, "check_same_thread=False", "SQLite cross-thread connection mode")
    require(db_runner, "db_lock = threading.RLock()", "incremental runner DB lock")
    require(db_runner, "counter_lock = threading.Lock()", "incremental runner counter lock")

    for path in (production, manual, smoke):
        content = read(path)
        require(path, "actions/checkout@v6", "Node 24 checkout action")
        require(path, "actions/setup-python@v7", "Node 24 setup-python action")
        if "actions/checkout@v4" in content or "actions/setup-python@v5" in content:
            raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} still references deprecated Node 20 action versions")
        if "actions/upload-artifact@v4" in content:
            raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} still references upload-artifact@v4")

    # ---- K 线分片懒加载契约 ----
    # 产物是 data_kline_manifest.js + data_kline_N.js，前端首次选中股票时才加载对应分片。
    for path in (production, manual):
        require(path, "data_kline_manifest.js", "K-line shard manifest handling")
        require(path, "data_kline*.js", "K-line shard glob restore")
        require(path, "ANNO_KLINE_SHARD", "K-line shard global")
        if 'src="data_kline.js"' in read(path):
            raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} still references the monolithic data_kline.js")

    require(index_html, "data_kline_manifest.js", "dashboard manifest script tag")
    require(index_html, "function ensureShard", "dashboard on-demand shard loader")
    if 'src="data_kline.js"' in read(index_html):
        raise SystemExit("[FAIL] dashboard still sync-loads the monolithic data_kline.js")
    print("[OK] dashboard loads K-lines on demand via shards")

    # ---- 股票池保留窗口 ----
    require(gen_dashboard, "POOL_RETENTION_DAYS", "stock pool retention window")
    require(gen_dashboard, "def pool_retention_days", "retention window resolver")

    # ---- 共享 build_universe：不允许再出现第二份实现 ----
    require(universe_mod, "def merge_archive", "shared archive merger")
    for path in (gen_dashboard, fetch_script, rule_script):
        require_import(path, "universe", "merge_archive", "shared archive merger import")
        if "def build_universe" in read(path) and "merge_archive(" not in read(path):
            raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} re-implements build_universe")

    # ---- 公告抓取必须并发 ----
    require(fetch_script, "ThreadPoolExecutor", "concurrent announcement fetch")
    require(fetch_script, "def _fetch_day", "per-day page fan-out")

    print("=== CI configuration validation PASSED ===")


if __name__ == "__main__":
    main()
