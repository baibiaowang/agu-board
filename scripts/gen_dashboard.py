"""
看板一键更新脚本：从当期 cninfo_announce_filtered.json 自动构建看板。
流程：读取筛选结果 → 按股票聚合公告事件 → 拉取K线（带重试）→ 注入事件 → 生成单文件 dashboard.html
用法：python scripts/gen_dashboard.py

GitHub Actions 适配：
- 支持从 data_archive 恢复历史数据
- 生成纯静态文件用于 GitHub Pages 部署
"""
import json, time, re, urllib.request, os
import sys
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root, is_github_actions

BASE = data_root()          # 可写数据根（exe旁 / project根）
RES = resource_root()       # 只读资源根（打包后 = _MEIPASS）
FILTERED = BASE / "cninfo_announce_filtered.json"
ARCHIVE = BASE / "cninfo_announce_archive"
DASH = BASE / "reports" / "dashboard"
MV_CACHE = BASE / "market_cap_cache.json"
LIB_ECHARTS = RES / "reports" / "dashboard" / "lib" / "echarts.min.js"

# GitHub Actions: 尝试从 data_archive 恢复历史数据
if is_github_actions():
    DATA_ARCHIVE = BASE / "data_archive"
    if DATA_ARCHIVE.exists():
        # 恢复市值缓存
        if (DATA_ARCHIVE / "market_cap_cache.json").exists() and not MV_CACHE.exists():
            try:
                import shutil
                shutil.copy2(DATA_ARCHIVE / "market_cap_cache.json", MV_CACHE)
                print("[GA] 恢复市值缓存从 data_archive")
            except Exception as e:
                print(f"[GA] 恢复市值缓存失败: {e}")
        # 恢复旧看板数据
        if (DATA_ARCHIVE / "data_list.js").exists():
            try:
                import shutil
                DASH.mkdir(parents=True, exist_ok=True)
                shutil.copy2(DATA_ARCHIVE / "data_list.js", DASH / "data_list.js")
                shutil.copy2(DATA_ARCHIVE / "data_kline.js", DASH / "data_kline.js")
                print("[GA] 恢复看板数据从 data_archive")
            except Exception as e:
                print(f"[GA] 恢复看板数据失败: {e}")

def build_universe(days=15, end_date=None):
    """合并近 days 天各次运行归档的筛选结果（去重），用于看板「近半月相关股票」展示。"""
    import datetime as _dt, re as _re
    end = end_date or _dt.date.today().strftime("%Y-%m-%d")
    try:
        cutoff = (_dt.date.fromisoformat(end) - _dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = "2000-01-01"
    merged, seen = [], set()
    files = sorted(ARCHIVE.glob("filtered_*.json")) if ARCHIVE.exists() else []
    for fp in files:
        m = _re.search(r"(\d{4}-\d{2}-\d{2})", fp.name)
        if not m:
            continue
        d = m.group(1)
        if d < cutoff or d > end:
            continue
        try:
            arr = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in arr:
            k = (a.get("code"), a.get("title"), a.get("time"))
            if k in seen:
                continue
            seen.add(k)
            merged.append(a)
    cur = FILTERED
    if cur.exists():
        try:
            for a in json.loads(cur.read_text(encoding="utf-8")):
                k = (a.get("code"), a.get("title"), a.get("time"))
                if k in seen:
                    continue
                seen.add(k)
                merged.append(a)
        except Exception:
            pass
    return merged

def secid(code):
    """东财 secid：沪市(60/688/689)用 1. 前缀，深市(00/30)用 0. 前缀。
    注意：北交所(83/87/88/43/92)东财无数据，fetch_kline 会直接跳过东财走腾讯源，不会用到 secid。"""
    if code.startswith(("60", "688", "689")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_market_cap(code):
    """腾讯实时行情接口拉取总市值（返回元）。字段45=总市值(亿元)。"""
    if code.startswith(("83", "87", "88", "43", "92")):
        tx = "bj" + code
    elif code.startswith("6"):
        tx = "sh" + code
    else:
        tx = "sz" + code
    url = "https://qt.gtimg.cn/q=" + tx
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk", errors="replace")
            parts = raw.split("~")
            if len(parts) > 45:
                try:
                    return float(parts[45]) * 1e8
                except Exception:
                    return 0
            return 0
    except Exception:
        return 0


def _atomic_write_text(path, text):
    """原子写：先写临时文件再 os.replace，避免目标被占用/写一半崩溃。
    目标被占用时 os.replace 会抛 PermissionError，但至少能给出清晰报错。"""
    d = os.path.dirname(str(path))
    os.makedirs(d, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, str(path))

def fetch_kline(code, lmt=120):
    """K线拉取：
    - 北交所(83/87/88/43/92 开头)：东财无数据，直接走腾讯源（newfqkline 接口，返回完整前复权日K）。
    - 沪深：优先东财，失败自动切腾讯源兜底。
    """
    is_bj = code.startswith(("83", "87", "88", "43", "92"))
    last_err = None
    # 1) 东财（仅沪深；北交所东财必失败，直接跳过省去 3 次无效重试）
    if not is_bj:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               f"secid={secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
               f"&klt=101&fqt=1&end=20500101&lmt={lmt}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "*/*",
            "Connection": "close",
        }
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
    # 2) 腾讯源兜底（newfqkline/get：proxy.finance.qq.com，返回完整前复权数据；
    #    旧 web.ifzq.gtimg.cn/fqkline/get 对北交所 qfqday 数据残缺，已弃用）
    if is_bj:
        tx_symbol = "bj" + code
    else:
        tx_symbol = ("sh" if code.startswith("6") else "sz") + code
    tx_url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?"
              f"param={tx_symbol},day,,,{lmt},qfq")
    tx_headers = {"User-Agent": "Mozilla/5.0 Chrome/126.0", "Referer": "https://gu.qq.com/"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(tx_url, headers=tx_headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                kd = res.get("data", {}).get(tx_symbol, {})
                kls_raw = kd.get("qfqday") or kd.get("day") or []
                if kls_raw:
                    # 转成东财同构格式 {"data": {"klines": [...]}}；qfqday 每条前6字段为
                    # [日期,开,收,高,低,量]（与东财一致），后续解析只取前6字段。
                    klines = [",".join(str(v) for v in k) for k in kls_raw]
                    return {"data": {"klines": klines, "name": code}}
                last_err = RuntimeError(f"{code} 无K线数据（可能停牌/转板）")
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise last_err

# 例行公告噪声标题（命中关键词但无看板价值，直接排除）
NOISE_TITLES = [
    "限制性股票", "回购注销", "回购价格", "行权价格", "法律意见书", "前十大股东",
    "债券", "兑付", "摘牌", "员工持股计划", "股本变动", "减少注册资本",
    "通知债权人", "审计报告", "业绩承诺补偿", "独立董事", "证券事务代表",
    "监事", "工作细则", "回购进展", "回购实施结果", "回购股份事项", "回购完成",
    "章程", "议事规则", "管理制度", "修正案",
]

def is_noise(title):
    t = title or ""
    for kw in NOISE_TITLES:
        if kw in t:
            return True
    # 质押类：仅保留控股股东/第一大股东/5%以上股东/解除质押，其余常规质押过滤
    if "质押" in t and not any(k in t for k in ["控股股东", "第一大股东", "5%以上股东", "解除质押", "解押"]):
        return True
    return False

def load_historical_data(days=90):
    """加载近N天所有历史数据（去重），用于累积显示。"""
    import datetime as _dt, re as _re
    end = _dt.date.today().strftime("%Y-%m-%d")
    try:
        cutoff = (_dt.date.fromisoformat(end) - _dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = "2000-01-01"
    
    merged, seen = [], set()
    files = sorted(ARCHIVE.glob("filtered_*.json")) if ARCHIVE.exists() else []
    for fp in files:
        m = _re.search(r"(\d{4}-\d{2}-\d{2})", fp.name)
        if not m:
            continue
        d = m.group(1)
        if d < cutoff or d > end:
            continue
        try:
            arr = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in arr:
            k = (a.get("code"), a.get("title"), a.get("time"))
            if k in seen:
                continue
            seen.add(k)
            merged.append(a)
    
    # 合并当期
    cur = FILTERED
    if cur.exists():
        try:
            for a in json.loads(cur.read_text(encoding="utf-8")):
                k = (a.get("code"), a.get("title"), a.get("time"))
                if k in seen:
                    continue
                seen.add(k)
                merged.append(a)
        except Exception:
            pass
    return merged

def main():
    if not FILTERED.exists():
        print("未找到 cninfo_announce_filtered.json，请先运行 scripts/cninfo_fetch.py")
        return

    filtered = json.loads(FILTERED.read_text(encoding="utf-8"))
    
    # 累积模式：加载近90天所有历史数据（去重），实现多期公告同时显示
    # 这样看板可以展示更长时间跨度的公告事件
    universe = load_historical_data(days=90)
    if universe:
        print(f"看板范围：当期 {len(filtered)} 条 + 近90天历史累积 {len(universe)} 条")
    else:
        universe = filtered
    
    # 去重 + 过滤例行噪声
    seen, items = set(), []
    for x in universe:
        key = (x.get("code"), x.get("title"))
        if key in seen:
            continue
        seen.add(key)
        if is_noise(x.get("title", "")):
            continue
        items.append(x)

    # 按股票聚合（保留 board/is_st，供前端切换）
    agg = defaultdict(lambda: {"code": "", "name": "", "category": "其他", "board": "", "is_st": False, "announcements": []})
    order = []
    for x in items:
        code = x.get("code")
        if not code:
            continue
        if code not in agg:
            agg[code]["code"] = code
            agg[code]["name"] = x.get("name", "")
            agg[code]["category"] = (x.get("cats") or ["其他"])[0]
            agg[code]["board"] = x.get("board", "")
            agg[code]["is_st"] = bool(x.get("is_st"))
            order.append(code)
        agg[code]["announcements"].append({
            "date": (x.get("time") or "")[:10],
            "title": x.get("title", ""),
            "board": x.get("board", ""),
            "is_st": bool(x.get("is_st")),
        })

    # 拉K线：已存在的股票沿用旧 data.js 的K线（不重复请求），仅新增股票拉取
    # 同时合并旧数据中的历史公告，确保增量更新不丢失历史
    old_data = {}
    old_js_path = DASH / "data_list.js"
    if old_js_path.exists():
        try:
            old_content = old_js_path.read_text(encoding="utf-8")
            s0 = old_content.index("[")
            e0 = old_content.rindex("]") + 1
            old_data = {o["code"]: o for o in json.loads(old_content[s0:e0])}
        except Exception:
            old_data = {}

    # 合并历史公告：将旧 data.js 中的公告合并到当前聚合结果
    # 这样即使某天没有新公告，历史公告仍然保留
    for code, old_item in old_data.items():
        if code not in agg:
            # 旧股票今天没有新公告，保留旧数据（包括K线和公告）
            agg[code] = {
                "code": code,
                "name": old_item.get("name", ""),
                "category": old_item.get("category", "其他"),
                "board": old_item.get("board", ""),
                "is_st": old_item.get("is_st", False),
                "announcements": list(old_item.get("announcements", []))
            }
            order.append(code)
        else:
            # 合并旧公告到新聚合结果（去重）
            existing_dates = {(a.get("date"), a.get("title")) for a in agg[code]["announcements"]}
            for old_ann in old_item.get("announcements", []):
                key = (old_ann.get("date"), old_ann.get("title"))
                if key not in existing_dates:
                    agg[code]["announcements"].append(old_ann)

    # 拉K线：新增股票全拉；存量股票若K线最后日期早于最近交易日也重新拉取（避免K线陈旧）
    import datetime as _dt
    _today = _dt.date.today().strftime("%Y-%m-%d")
    def _kline_stale(code):
        kl = old_data[code].get("klines") if code in old_data else None
        if not kl:
            return True  # 无旧K线，需要拉取
        return kl[-1][0] < _today  # K线最后日期早于今天 → 陈旧，重新拉取
    codes_to_fetch = [c for c in order if _kline_stale(c)]
    kline_results = {}
    if codes_to_fetch:
        def pull_kline(code):
            try:
                res = fetch_kline(code)
                d = res.get("data") or {}
                name_east = d.get("name", "")
                klines = [[k.split(",")[0], float(k.split(",")[1]), float(k.split(",")[2]),
                           float(k.split(",")[3]), float(k.split(",")[4]), float(k.split(",")[5])]
                          for k in (d.get("klines") or [])]
                mv = 0
                return code, klines, name_east, mv, None
            except Exception as e:
                return code, [], "", 0, str(e)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(pull_kline, c) for c in codes_to_fetch]
            for f in as_completed(futs):
                code, klines, name_east, mv, err = f.result()
                if err:
                    print(f"  K线拉取失败 {code}: {err}", flush=True)
                kline_results[code] = (klines, name_east, mv)

    # 拉取市值：优先读本地缓存，仅对缺失股票拉取（市值变动不大，基本不更新，缓存一次即可）
    def _load_mv_cache():
        try:
            return json.loads(MV_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    def _save_mv_cache(d):
        try:
            MV_CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        # GitHub Actions: 同时保存到 data_archive
        if is_github_actions():
            try:
                archive_dir = BASE / "data_archive"
                archive_dir.mkdir(parents=True, exist_ok=True)
                (archive_dir / "market_cap_cache.json").write_text(
                    json.dumps(d, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass
    mv_cache = _load_mv_cache()
    missing = [c for c in order if c not in mv_cache]
    if missing:
        def pull_mv(code):
            try:
                return code, fetch_market_cap(code)
            except Exception:
                return code, 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(pull_mv, c) for c in missing]
            for f in as_completed(futs):
                code, mv = f.result()
                if mv > 0:
                    mv_cache[code] = mv
        _save_mv_cache(mv_cache)
    mv_map = {c: mv_cache.get(c, 0) for c in order}

    data = []
    for idx, code in enumerate(order):
        s = agg[code]
        klines, name_east, mv = [], "", 0
        if code in kline_results and kline_results[code][0]:
            klines, name_east, mv = kline_results[code]  # 优先用新拉取的
        elif code in old_data and old_data[code].get("klines"):
            klines = old_data[code]["klines"]  # 拉取失败回退旧K线
            name_east = old_data[code].get("name_east", "")
            mv = old_data[code].get("market_cap", 0)
        # 保留所有历史公告（不限于4条），按日期排序
        anns = sorted([a for a in s["announcements"] if a["date"]], key=lambda a: a["date"])
        reason = anns[-1]["title"] if anns else "入选本期公告"
        # 截断到最近60个交易日（减少前端下载与渲染数据量）
        if len(klines) > 60:
            klines = klines[-60:]
        # 预计算涨跌幅（前端渲染不再重复遍历K线，大幅提速）
        chg = 0; chg5 = 0; chg_ann = None
        if len(klines) >= 2:
            _p, _c = klines[-2][2], klines[-1][2]
            chg = round((_c - _p) / _p * 100, 2) if _p else 0
        if len(klines) >= 6:
            _p5 = klines[-6][2]
            chg5 = round((_c - _p5) / _p5 * 100, 2) if _p5 else 0
        if klines and anns:
            _di = {k[0]: i for i, k in enumerate(klines)}
            _idx = -1
            for _a in anns:
                if _a["date"] in _di:
                    _idx = _di[_a["date"]]; break
            if _idx >= 0:
                _base = klines[_idx-1][2] if _idx > 0 else klines[_idx][1]
                chg_ann = round((_c - _base) / _base * 100, 2) if _base else None
        data.append({
            "code": code, "name": s["name"], "category": s["category"],
            "board": s.get("board", ""), "is_st": s.get("is_st", False),
            "reason": reason, "klines": klines, "announcements": anns,
            "name_east": name_east,
            "chg": chg, "chg5": chg5, "chg_ann": chg_ann,
            "market_cap": mv_map.get(code, mv),
        })

    # 日期范围（用于标题）
    all_dates = [a["date"] for s in data for a in s["announcements"] if a["date"]]
    date_range = f"{min(all_dates)} ~ {max(all_dates)}" if all_dates else ""

    # 拆分写数据：列表数据(文字+公告) + K线数据(按code)，实现文字先加载、K线懒加载
    DASH.mkdir(parents=True, exist_ok=True)
    list_data = []
    kline_map = {}
    for s in data:
        list_data.append({
            "code": s["code"], "name": s["name"], "category": s["category"],
            "board": s.get("board", ""), "is_st": s.get("is_st", False),
            "reason": s.get("reason", ""), "chg": s.get("chg", 0),
            "chg5": s.get("chg5", 0), "chg_ann": s.get("chg_ann"),
            "market_cap": s.get("market_cap", 0),
            "announcements": s.get("announcements", []),
        })
        kline_map[s["code"]] = s.get("klines", [])
    js_list = "window.ANNO_LIST = " + json.dumps(list_data, ensure_ascii=False) + ";\n"
    js_kline = "window.ANNO_KLINE = " + json.dumps(kline_map, ensure_ascii=False) + ";\n"
    try:
        _atomic_write_text(DASH / "data_list.js", js_list)
        _atomic_write_text(DASH / "data_kline.js", js_kline)
    except Exception as e:
        raise RuntimeError(f"写入 data 文件失败（可能是文件被浏览器占用，或目录无写权限）：{e}") from e

    # GitHub Actions: 同时保存到 data_archive 以便下次恢复
    if is_github_actions():
        try:
            archive_dir = BASE / "data_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(DASH / "data_list.js", archive_dir / "data_list.js")
            shutil.copy2(DASH / "data_kline.js", archive_dir / "data_kline.js")
        except Exception:
            pass

    # 生成单文件 dashboard.html
    # 模板 index.html 与 echarts.min.js 为只读资源，打包后位于 _MEIPASS，故用 RES 定位
    if not (RES / "reports" / "dashboard" / "index.html").exists() or not LIB_ECHARTS.exists():
        raise FileNotFoundError("缺少看板模板 index.html 或 lib/echarts.min.js，请确认工程完整。")
    html = (RES / "reports" / "dashboard" / "index.html").read_text(encoding="utf-8")
    echarts = LIB_ECHARTS.read_text(encoding="utf-8")
    # 更新标题日期范围
    if date_range:
        html = re.sub(r"<div class=\"sub\">[^<]*</div>",
                      f'<div class="sub">{date_range} · 巨潮资讯全量公告 · 默认主板(不含ST)，可切换板块查看</div>', html)
        html = re.sub(r"<title>[^<]*</title>", f"<title>A股公告看板（{date_range}）</title>", html)
    # 不再内联 echarts 和 data.js，保留外部引用（拆分后浏览器可缓存，二次打开秒开）
    try:
        _atomic_write_text(DASH / "dashboard.html", html)
    except Exception as e:
        raise RuntimeError(f"写入 dashboard.html 失败（可能是文件被浏览器占用，请关闭已打开的同名页面后重试）：{e}") from e

    print(f"看板更新完成: {len(data)} 只股票, 日期范围: {date_range}")
    print(f"  无K线数据: {[s['code'] for s in data if not s['klines']]}")

if __name__ == "__main__":
    main()
