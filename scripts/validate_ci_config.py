"""Static validation for the GitHub Actions contract used by agu-board."""
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


def require_any(path: Path, texts: tuple[str, ...], label: str) -> None:
    content = read(path)
    if not any(text in content for text in texts):
        raise SystemExit(
            f"[FAIL] {path.relative_to(ROOT)} missing {label}: expected one of {texts!r}"
        )
    print(f"[OK] {path.relative_to(ROOT)}: {label}")


def main() -> None:
    production = WF / "update-board.yml"
    manual = WF / "manual-incremental-update.yml"
    smoke = WF / "smoke-test.yml"

    for path in (production, manual, smoke):
        if not path.exists():
            raise SystemExit(f"[FAIL] missing workflow: {path.relative_to(ROOT)}")

    require(production, "name: Update A-Share Announcement Board", "friendly workflow name")
    require(production, "workflow_dispatch:", "manual dispatch")
    require(production, "timezone: 'Asia/Shanghai'", "explicit Asia/Shanghai schedule timezone")
    require_any(
        production,
        ("'source': 'eastmoney'", '"source": "eastmoney"', "source: 'eastmoney'"),
        "Eastmoney runtime status",
    )
    require(production, "group: announcement-board", "shared concurrency group")
    require(production, "scripts/eastmoney_fetch.py", "Eastmoney fetch path")
    require(production, "mainboard_tool/rule_summarize.py", "rule report path")
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
    require(manual, "mainboard_tool/rule_summarize.py", "manual rule report path")

    require(smoke, "End-to-End Smoke Test", "smoke workflow")
    require(smoke, "scripts/eastmoney_fetch.py", "Eastmoney smoke path")
    require(smoke, "mainboard_tool/extract_auto.py", "content extraction smoke path")
    require(smoke, "scripts/gen_dashboard.py", "dashboard smoke path")

    for path in (production, manual, smoke):
        content = read(path)
        require(path, "actions/checkout@v6", "Node 24 checkout action")
        require(path, "actions/setup-python@v7", "Node 24 setup-python action")
        if "actions/checkout@v4" in content or "actions/setup-python@v5" in content:
            raise SystemExit(
                f"[FAIL] {path.relative_to(ROOT)} still references deprecated Node 20 action versions"
            )
        if "actions/upload-artifact@v4" in content:
            raise SystemExit(
                f"[FAIL] {path.relative_to(ROOT)} still references upload-artifact@v4"
            )

    print("=== CI configuration validation PASSED ===")


if __name__ == "__main__":
    main()
