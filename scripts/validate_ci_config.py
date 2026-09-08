"""Static validation for the GitHub Actions contract used by agu-board."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def require(path: Path, text: str, label: str) -> None:
    content = path.read_text(encoding="utf-8")
    if text not in content:
        raise SystemExit(f"[FAIL] {path.relative_to(ROOT)} missing {label}: {text!r}")
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
    require(production, "source: 'eastmoney'", "Eastmoney runtime status")
    require(production, "group: announcement-board", "shared concurrency group")
    if "git push origin main" in production.read_text(encoding="utf-8"):
        raise SystemExit("[FAIL] production workflow must not self-push to main")
    print("[OK] production workflow does not self-push main")

    require(manual, "name: Manual Incremental Announcement Update", "friendly manual workflow name")
    require(manual, "on:\n  workflow_dispatch:", "manual-only trigger")
    require(manual, "mode': 'incremental", "incremental mode status")
    require(manual, "manual-incremental-update.yml", "self-service dashboard target")
    require(manual, "PRE-DEPLOY HEALTH: OK", "pre-deploy health gate")

    require(smoke, "End-to-End Smoke Test", "smoke workflow")
    require(smoke, "scripts/eastmoney_fetch.py", "Eastmoney smoke path")
    require(smoke, "mainboard_tool/extract_auto.py", "content extraction smoke path")
    require(smoke, "scripts/gen_dashboard.py", "dashboard smoke path")

    print("=== CI configuration validation PASSED ===")


if __name__ == "__main__":
    main()
