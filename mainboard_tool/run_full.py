"""完整编排器：东方财富公告 → PDF正文 → 规则报告 → K线看板。"""
import sys
import os
import json
from pathlib import Path
from datetime import date, timedelta, datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_TOOL = Path(__file__).resolve().parent
_PROJ = _TOOL.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root

BASE = data_root()
TOOL = _TOOL
SCRIPTS = resource_root() / "scripts"
STATE_PATH = BASE / "cninfo_announce_state.json"
for _p in (str(BASE), str(TOOL), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(SCRIPTS) in sys.path:
    sys.path.remove(str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

import eastmoney_fetch
import extract_auto
import rule_summarize
import gen_dashboard


def beijing_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def last_trading_day(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def default_window():
    end = last_trading_day(beijing_today())
    start = end - timedelta(days=3)
    try:
        if STATE_PATH.exists():
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            last = st.get("last_end_date")
            if last:
                start = datetime.strptime(last, "%Y-%m-%d").date()
    except Exception:
        pass
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


class _Tee:
    def __init__(self, real, log):
        self.real = real
        self.log = log

    def write(self, s):
        try:
            self.real.write(s)
        except Exception:
            pass
        if self.log is not None and s and s.strip():
            for line in s.rstrip().splitlines():
                self.log(line)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass


def _stage(log, label, fn):
    """执行单阶段；失败记录日志但不阻断后续可以独立工作的阶段。"""
    try:
        log(f"{label}开始")
        result = fn()
        log(f"{label}完成")
        return True, result, None
    except Exception as e:
        import traceback
        log(f"⚠️ {label}失败：{e}")
        log(traceback.format_exc())
        return False, None, e


def run(start=None, end=None, log=None, full_rescan=False, preset=None):
    real_stdout = sys.stdout
    progress = log if log is not None else lambda m: real_stdout.write(m + "\n")
    if not start or not end:
        if preset and preset in eastmoney_fetch.RANGE_PRESETS:
            start, end = eastmoney_fetch.window_from_preset(preset)
        elif full_rescan:
            end = beijing_today().strftime("%Y-%m-%d")
            start = (beijing_today() - timedelta(days=90)).strftime("%Y-%m-%d")
        else:
            start, end = default_window()
    mode = ("【档位·" + preset + "】") if preset else ("【全量重扫】" if full_rescan else "【增量/记忆模式】")
    progress(f"{mode} 数据窗口：{start} ~ {end}")

    old_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout, log) if log is not None else real_stdout
    results = {}
    errors = []
    try:
        ok, value, err = _stage(progress, "[1/4] 东方财富公告采集", lambda: eastmoney_fetch.run_fetch(start, end, full_rescan=full_rescan, preset=preset))
        results["fetch"] = ok
        if err:
            errors.append(("公告采集", err))

        # 即使本次网络采集失败，也继续使用工作目录里上一份有效 filtered 数据生成报告/看板。
        ok, _, err = _stage(progress, "[2/4] 东方财富公告 PDF 原文提取", extract_auto.main)
        results["extract"] = ok
        if err:
            errors.append(("PDF提取", err))

        ok, _, err = _stage(progress, "[3/4] 规则报告生成", lambda: rule_summarize.main(start, end))
        results["report"] = ok
        if err:
            errors.append(("报告生成", err))

        ok, _, err = _stage(progress, "[4/4] K线看板生成", gen_dashboard.main)
        results["dashboard"] = ok
        if err:
            errors.append(("看板生成", err))
    finally:
        sys.stdout = old_stdout

    report = str(BASE / "reports" / f"A股主板公告总结_{start}_{end}.md")
    dashboard = str(BASE / "reports" / "dashboard" / "dashboard.html")
    progress(f"完成阶段：{', '.join(k for k,v in results.items() if v) or '无'}")
    if errors:
        progress("⚠️ 本次运行存在阶段性错误，但程序未因单个阶段失败而整体退出：")
        for name, err in errors:
            progress(f"  - {name}: {err}")
    if Path(report).exists():
        progress("✅ 报告：" + report)
    else:
        progress("⚠️ 报告未生成：" + report)
    if Path(dashboard).exists():
        progress("✅ 看板：" + dashboard)
    else:
        progress("⚠️ 看板未生成：" + dashboard)
    return {
        "report": report if Path(report).exists() else None,
        "dashboard": dashboard if Path(dashboard).exists() else None,
        "start": start,
        "end": end,
        "stages": results,
        "errors": [name for name, _ in errors],
    }


if __name__ == "__main__":
    args = sys.argv[1:]
    start = args[0] if len(args) >= 2 and not args[0].startswith("--") else None
    end = args[1] if len(args) >= 2 and not args[1].startswith("--") else None
    full = "--full" in args
    preset = next((x for x in eastmoney_fetch.RANGE_PRESETS if x in args), None)
    res = run(start, end, full_rescan=full, preset=preset)
    if res and "--no-open" not in args:
        try:
            if res.get("dashboard") and os.name == "nt":
                os.startfile(res["dashboard"])
            if res.get("report") and os.name == "nt":
                os.startfile(res["report"])
        except Exception as ex:
            print("自动打开失败，请手动打开：", ex)
