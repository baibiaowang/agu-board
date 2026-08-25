"""编排器（无token版，进程内调用）
串联：巨潮拉取 → 自动抽取PDF原文 → 规则化总结 → 更新K线看板。
计算默认交易日窗口（最近交易日-3 ~ 最近交易日，含前一晚及周末），支持命令行参数，
运行结束自动打开报告与看板。被 server.py 与一键bat复用。

关键设计：直接 import 各脚本的入口函数（而非 subprocess 调外部 python），
这样 PyInstaller 打包成单文件 exe 后依然可运行（exe 内没有独立 python.exe）。
各脚本的 print 输出通过 sys.stdout 重定向回传给 log 回调，供网页 UI 实时展示。
"""
import sys, os, json, io
from pathlib import Path
from datetime import date, timedelta, datetime

# 修复打包(--windowed 无控制台)后 stdout 编码退化为 GBK，导致 emoji print 报 UnicodeEncodeError
# （源码运行时有 PYTHONIOENCODING=utf-8，但 exe 双击无环境变量）。强制重配为 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 路径统一：打包(exe)后 __file__ 指向临时解压目录(_MEIPASS)，需区分可写数据与只读资源
_TOOL = Path(__file__).resolve().parent
_PROJ = _TOOL.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root, is_frozen

BASE = data_root()                                    # 可写数据根（exe旁 / project根）
TOOL = _TOOL                                          # mainboard_tool/（源码）
SCRIPTS = resource_root() / "scripts"                 # 脚本目录（源码=project/scripts；打包=_MEIPASS/scripts）
STATE_PATH = BASE / "cninfo_announce_state.json"

# 把 scripts 与 mainboard_tool 加入 import 路径，供进程内调用
for _p in (str(BASE), str(TOOL), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# 关键：强制 SCRIPTS 绝对位于 sys.path 最前。
# 根目录(BASE)可能残留旧版同名脚本(cninfo_fetch.py 顶层会直接联网)，
# 若 desktop.py/server.py 已预先 insert 过 scripts，上面的 not-in 判断会跳过 SCRIPTS，
# 导致 BASE 被 insert(0) 顶到最前，import cninfo_fetch 命中旧版卡死。故显式重排。
if str(SCRIPTS) in sys.path:
    sys.path.remove(str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

import cninfo_fetch        # run_fetch(start, end, full_rescan)
import extract_auto        # main()
import rule_summarize      # main(start, end)
import gen_dashboard       # main()


def last_trading_day(d):
    while d.weekday() >= 5:  # 5=周六 6=周日
        d -= timedelta(days=1)
    return d


def default_window():
    """记忆/增量：有上次记录则从「上次结束日」续抓；否则默认最近交易日-3 ~ 最近交易日。"""
    end = last_trading_day(date.today())
    start = end - timedelta(days=3)  # 覆盖最近一个交易日+前一晚及周末(含周六)
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
    """把子模块 print 的输出同时写到真实 stdout（控制台），并可选用 log 回调转发（供网页 UI 收集）。
    设计上 log 只在 server.py 场景传入（append 到 list，不 print），CLI 场景传 None，
    从而彻底避免 print→write→print 的递归。"""
    def __init__(self, real, log):
        self.real = real
        self.log = log

    def write(self, s):
        try:
            self.real.write(s)
        except Exception:
            # windowed exe 下 real stdout 可能编码异常（GBK）或指向 NUL，静默降级；
            # 真实输出由 log 回调（server 场景）或 reconfigure 后的 UTF-8 流承担。
            pass
        if self.log is not None and s and s.strip():
            for line in s.rstrip().splitlines():
                self.log(line)

    def flush(self):
        self.real.flush()


def run(start=None, end=None, log=None, full_rescan=False, preset=None):
    """log 为可选回调（网页 UI 传入收集函数；CLI 传 None，此时只写真实 stdout）。
    preset 为时间档位名称（3天/一周/半个月/一个月），优先级低于显式 start/end。
    返回 dict（含 report/dashboard 路径）或 None（失败）。"""
    real_stdout = sys.stdout
    if log is None:
        progress = lambda m: real_stdout.write(m + "\n")
        tee_log = None
    else:
        progress = log
        tee_log = log
    if not start or not end:
        if preset and preset in cninfo_fetch.RANGE_PRESETS:
            start, end = cninfo_fetch.window_from_preset(preset)
        elif full_rescan:
            end = date.today().strftime("%Y-%m-%d")
            start = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
        else:
            start, end = default_window()
    mode = ("【档位·" + preset + "】") if preset else ("【全量重扫】" if full_rescan else "【增量/记忆模式】")
    progress(f"{mode} 拉取窗口: {start} ~ {end}")

    old_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout, tee_log)
    dash_ok = True
    dash_err = None
    try:
        progress("[1/4] 拉取巨潮全量公告 ...")
        cninfo_fetch.run_fetch(start, end, full_rescan=full_rescan)

        progress("[2/4] 自动抽取重点公告PDF原文 ...")
        extract_auto.main()

        progress("[3/4] 规则引擎生成总结报告 ...")
        rule_summarize.main(start, end)

        progress("[4/4] 更新K线看板 ...")
        try:
            gen_dashboard.main()
        except Exception as e:
            dash_ok = False
            dash_err = e
            progress("⚠️ 看板更新失败（不影响报告）：" + str(e))
    except Exception as e:
        progress("!! 运行异常: " + str(e))
        import traceback
        progress(traceback.format_exc())
        return None
    finally:
        sys.stdout = old_stdout

    report = str(BASE / "reports" / f"A股主板公告总结_{start}_{end}.md")
    dash = str(BASE / "reports" / "dashboard" / "dashboard.html")
    progress("✅ 完成！报告：" + report)
    if dash_ok:
        progress("✅ 看板：" + dash)
    else:
        progress("⚠️ 看板未生成（" + str(dash_err) + "）")
    return {"report": report, "dashboard": dash if dash_ok else None, "start": start, "end": end}


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else ""
    e = sys.argv[2] if len(sys.argv) > 2 else ""
    full = "--full" in sys.argv
    preset = None
    for _p in cninfo_fetch.RANGE_PRESETS:
        if _p in sys.argv:
            preset = _p
            break
    res = run(s, e, full_rescan=full, preset=preset)
    if res and "--no-open" not in sys.argv:
        try:
            if res.get("dashboard"):
                os.startfile(res["dashboard"])
            os.startfile(res["report"])
        except Exception as ex:
            print("自动打开失败，请手动打开：", ex)
