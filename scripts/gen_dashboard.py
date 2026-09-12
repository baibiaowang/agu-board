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
from universe import display_title, merge_archive, norm_title

BASE = data_root()          # 可写数据根（exe旁 / project根）
RES = resource_root()       # 只读资源根（打包后 = _MEIPASS）
FILTERED = BASE / "cninfo_announce_filtered.json"
ARCHIVE = BASE / "cninfo_announce_archive"
DASH = BASE / "reports" / "dashboard"
MV_CACHE = BASE / "market_cap_cache.json"
LIB_ECHARTS = RES / "reports" / "dashboard" / "lib" / "echarts.min.js"

# 看板只渲染最近 60 个交易日。拉取与截断共用这一个常量，
# 避免「请求 120 根、最后只留 60 根」这种白跑一半的情况。
KLINE_BARS = 60

# K 线产物按「股票代码取模」分成 KLINE_SHARDS 个分片文件（data_kline_N.js），
# 前端首次选中某只股票时才加载它所属的那一片。分片号由代码本身决定，
# 前端无需额外清单即可算出，manifest 只用于声明分片数量。
# 改动前是单个 data_kline.js，已达 12.29 MB 且被 <script> 同步加载。
KLINE_SHARDS = 16

# 股票池保留窗口（天）。旧 data_list.js 里的股票若在窗口内没有任何公告就不再带入，
# 其公告列表同样裁剪到窗口内。没有这条策略时股票池只增不减，
# K 线产物与列表产物会单调膨胀。
POOL_RETENTION_DAYS = 90

# 每只股票在看板上保留的公告条数上限；0 = 不限制（默认，保持既有行为）。
# 这是给「股票池并入数据库」准备的安全阀：库内 90 天窗口有约 9 万条公告，
# 其中约 79% 是 classify() 未命中的例行公告，若不加筛选全量输出，
# data_list.js 会从 2.5 MB 涨到 14 MB（且是同步 <script> 加载），
# gh-pages 分支一年会膨胀到 2 GB 以上。
ANNO_MAX_PER_STOCK = 0

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
        # 恢复旧看板数据（K 线为分片文件，兼容早期归档里的单文件 data_kline.js）
        if (DATA_ARCHIVE / "data_list.js").exists():
            try:
                import shutil
                DASH.mkdir(parents=True, exist_ok=True)
                shutil.copy2(DATA_ARCHIVE / "data_list.js", DASH / "data_list.js")
                for src in sorted(DATA_ARCHIVE.glob("data_kline*.js")):
                    shutil.copy2(src, DASH / src.name)
                print("[GA] 恢复看板数据从 data_archive")
            except Exception as e:
                print(f"[GA] 恢复看板数据失败: {e}")

def shard_of(code) -> int:
    """分片号。前端用同一公式算，因此分片不需要额外的映射表。"""
    try:
        return int(str(code).strip()) % KLINE_SHARDS
    except (TypeError, ValueError):
        return 0


def _parse_js_object(text: str) -> dict:
    """从 `window.XXX = {...};` 中取出对象；解析失败返回空 dict。"""
    try:
        s0 = text.index("{")
        e0 = text.rindex("}") + 1
        obj = json.loads(text[s0:e0])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def load_previous_klines(dash_dir):
    """读取上一轮产出的 K 线（分片文件，兼容旧的单文件 data_kline.js）。

    这是「K 线陈旧判定」的前提。改动前这里只读 data_list.js，而 data_list.js
    并不包含 klines 字段，于是 _kline_stale() 对每只股票都返回 True，
    整个股票池每一轮都会被判定为需要刷新——超时的放大器之一。
    """
    merged = {}
    legacy = dash_dir / "data_kline.js"
    if legacy.exists():
        try:
            merged.update(_parse_js_object(legacy.read_text(encoding="utf-8")))
        except OSError:
            pass
    for i in range(KLINE_SHARDS):
        fp = dash_dir / f"data_kline_{i}.js"
        if not fp.exists():
            continue
        try:
            merged.update(_parse_js_object(fp.read_text(encoding="utf-8")))
        except OSError:
            continue
    return merged


def write_kline_shards(dash_dir, kline_map) -> dict:
    """写出分片 K 线 + manifest，并清掉旧的单文件产物。"""
    buckets = {}
    for code, klines in kline_map.items():
        buckets.setdefault(shard_of(code), {})[code] = klines
    manifest = {"shards": KLINE_SHARDS, "codes": len(kline_map), "bars": KLINE_BARS}
    _atomic_write_text(
        dash_dir / "data_kline_manifest.js",
        "window.ANNO_KLINE_SHARDS = " + json.dumps(manifest, ensure_ascii=False) + ";\n",
    )
    for i in range(KLINE_SHARDS):
        _atomic_write_text(
            dash_dir / f"data_kline_{i}.js",
            f"window.ANNO_KLINE_SHARD_{i} = "
            + json.dumps(buckets.get(i, {}), ensure_ascii=False)
            + ";\n",
        )
    # 旧的单文件产物已不再被 index.html 引用，留着只会被部署并归档，白占 12MB+。
    (dash_dir / "data_kline.js").unlink(missing_ok=True)
    return manifest


def pool_retention_days() -> int:
    """股票池保留窗口天数，可用 POOL_RETENTION_DAYS 覆盖。"""
    try:
        return max(1, int(os.environ.get("POOL_RETENTION_DAYS", str(POOL_RETENTION_DAYS))))
    except (TypeError, ValueError):
        return POOL_RETENTION_DAYS


def retention_from(days: int) -> str:
    """保留窗口起始日（本地日期，与 load_historical_data 的窗口口径一致）。"""
    import datetime as _dt
    return (_dt.date.today() - _dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")


def announcement_limit() -> int:
    """每只股票保留的公告条数上限，可用 ANNO_MAX_PER_STOCK 覆盖；0 表示不限制。"""
    try:
        return max(0, int(os.environ.get("ANNO_MAX_PER_STOCK", str(ANNO_MAX_PER_STOCK))))
    except (TypeError, ValueError):
        return ANNO_MAX_PER_STOCK


def normalize_anns(anns, name=""):
    """归一化从上一轮 data_list.js 继承的公告列表。

    上一轮产物里的标题可能带「简称:」前缀或用全角标点，与本轮来源（归档不带前缀、
    库内带前缀）写法不一致。不归一化就会把同一条公告算成两条并排显示——实测会
    多出上百条重复。这里同时做两件事：按归一化标题去重、输出标题统一剥前缀。
    """
    out, seen = [], set()
    for a in anns or []:
        if not isinstance(a, dict):
            continue
        key = (a.get("date"), norm_title(a.get("title"), name))
        if key in seen:
            continue
        seen.add(key)
        out.append({**a, "title": display_title(a.get("title"), name)})
    return out

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

def fetch_kline(code, lmt=KLINE_BARS):
    """K线拉取：
    - 北交所(83/87/88/43/92 开头)：东财无数据，直接走腾讯源（newfqkline 接口，返回完整前复权日K）。
    - 沪深：优先东财，失败自动切腾讯源兜底。
    默认只取 KLINE_BARS 根：下游本来就按这个数截断，多取只会放大网络与序列化开销。
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
    """加载近 N 天归档 + 当期 filtered 的全部公告（去重），用于累积显示。

    实现已统一到 universe.merge_archive：原先本文件与 rule_summarize.py、
    eastmoney_fetch.py 各有一份同名逻辑，三份在窗口边界与容错上并不一致。
    """
    return merge_archive(ARCHIVE, days, None, extra_files=[FILTERED])


def load_universe(days=90):
    """看板股票池来源。

    默认只用归档目录。gen_dashboard_incremental 在拿到 SQLite 基线后会把它
    替换成「归档 ∪ 库」的实现——归档目录只有 08-25 起十几个 filtered_*.json，
    而库里有约 90 天的公告；实测并集比单用归档多 216 只股票（+5.5%），
    产物只增加约 0.4 MB。库不可用时这里保持原行为，不做任何额外假设。
    """
    return load_historical_data(days)

def main():
    if not FILTERED.exists():
        print("未找到 cninfo_announce_filtered.json，请先运行 scripts/cninfo_fetch.py")
        return

    filtered = json.loads(FILTERED.read_text(encoding="utf-8"))
    
    # 累积模式：加载近90天所有历史数据（去重），实现多期公告同时显示
    # 这样看板可以展示更长时间跨度的公告事件
    # 来源可被 gen_dashboard_incremental 替换为「归档 ∪ 库」（见 load_universe）。
    universe = load_universe(days=90)
    if universe:
        print(f"看板范围：当期 {len(filtered)} 条 + 近90天历史累积 {len(universe)} 条")
    else:
        universe = filtered
    
    # 去重 + 过滤例行噪声。
    # 去重键用归一化标题：股票池可能同时来自归档与数据库，两侧标题写法不同
    # （库内带「简称:」前缀、标点可能是半角），用原始标题比对会漏掉重复。
    seen, items = set(), []
    for x in universe:
        key = (x.get("code"), norm_title(x.get("title"), x.get("name")))
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

    # 旧数据有两个来源，缺一不可：
    #   data_list.js       → 列表字段（名称/板块/类别/公告）
    #   data_kline_N.js    → 上一轮产出的 K 线
    # 只读 data_list.js 是原先的写法，而该文件并不含 klines 字段，
    # 于是下面的 _kline_stale() 对每只股票都返回 True，整个股票池每轮都被判为需刷新。
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
    prev_klines = load_previous_klines(DASH)
    for code, klines in prev_klines.items():
        old_data.setdefault(code, {"code": code})["klines"] = klines
    if prev_klines:
        print(f"复用上一轮 K 线：{len(prev_klines):,} 只")

    # 合并历史公告：将旧 data_list.js 中的公告合并到当前聚合结果，
    # 这样即使某天没有新公告，历史公告仍然保留。
    #
    # 同时施加股票池保留窗口：窗口内没有任何公告的旧股票不再带入，
    # 其公告列表也裁剪到窗口内。没有这条策略时股票池只增不减，
    # K 线产物与列表产物会单调膨胀。
    retain_days = pool_retention_days()
    retain_from = retention_from(retain_days)

    def _recent(anns):
        return [a for a in (anns or []) if str(a.get("date") or "") >= retain_from]

    dropped_stocks = trimmed_anns = carried_stocks = 0
    for code, old_item in old_data.items():
        old_anns = _recent(old_item.get("announcements"))
        trimmed_anns += len(old_item.get("announcements") or []) - len(old_anns)
        if code not in agg:
            if not old_anns:
                # 窗口内没有任何公告：既不该出现在看板上，也不再带入它的 K 线。
                dropped_stocks += 1
                continue
            _nm = old_item.get("name", "")
            agg[code] = {
                "code": code,
                "name": _nm,
                "category": old_item.get("category", "其他"),
                "board": old_item.get("board", ""),
                "is_st": old_item.get("is_st", False),
                # 整包继承，同样要归一化：上一轮产物内部就可能存在带前缀与不带前缀的
                # 两种写法，不归一化会原样带上重复。
                "announcements": normalize_anns(old_anns, _nm),
            }
            order.append(code)
            carried_stocks += 1
        else:
            # 合并旧公告到新聚合结果（去重）。
            # 键同样必须用归一化标题：上一轮 data_list.js 里的写法可能是
            # 「简称:标题」或全角标点，与本轮来源不一致，用原始标题比对会把
            # 同一条公告重复带入（实测会多出上百条并排重复）。
            _nm = agg[code].get("name", "")
            existing = {(a.get("date"), norm_title(a.get("title"), _nm))
                        for a in agg[code]["announcements"]}
            for old_ann in normalize_anns(old_anns, _nm):
                key = (old_ann.get("date"), norm_title(old_ann.get("title"), _nm))
                if key not in existing:
                    existing.add(key)
                    agg[code]["announcements"].append(old_ann)
    print(
        f"股票池保留窗口 {retain_days} 天（{retain_from} 起）："
        f"淘汰窗口外股票 {dropped_stocks} 只，裁剪过期公告 {trimmed_anns} 条，"
        f"沿用窗口内历史股票 {carried_stocks} 只"
    )

    # 拉K线：新增股票全拉；存量股票若K线最后日期早于最近交易日也重新拉取（避免K线陈旧）
    # 基准必须是「最近交易日」而不是自然日：K 线最后一根只可能落在交易日，用
    # date.today() 比较会让周末 / 节假日 / 盘前的每一只股票都判定为陈旧，
    # 把整个股票池（数千只）都塞进拉取队列。
    import datetime as _dt
    _baseline = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date()
    while _baseline.weekday() >= 5:
        _baseline -= _dt.timedelta(days=1)
    _baseline = _baseline.strftime("%Y-%m-%d")
    def _kline_stale(code):
        kl = old_data[code].get("klines") if code in old_data else None
        if not kl:
            return True  # 无旧K线，需要拉取
        return kl[-1][0] < _baseline  # K线最后日期早于最近交易日 → 陈旧，重新拉取
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
        # 保留所有历史公告，按日期排序
        anns_all = sorted([a for a in s["announcements"] if a["date"]], key=lambda a: a["date"])
        reason = anns_all[-1]["title"] if anns_all else "入选本期公告"
        # 截断到最近 KLINE_BARS 个交易日（减少前端下载与渲染数据量）
        if len(klines) > KLINE_BARS:
            klines = klines[-KLINE_BARS:]
        # 预计算涨跌幅（前端渲染不再重复遍历K线，大幅提速）
        chg = 0; chg5 = 0; chg_ann = None
        if len(klines) >= 2:
            _p, _c = klines[-2][2], klines[-1][2]
            chg = round((_c - _p) / _p * 100, 2) if _p else 0
        if len(klines) >= 6:
            _p5 = klines[-6][2]
            chg5 = round((_c - _p5) / _p5 * 100, 2) if _p5 else 0
        # chg_ann 用完整公告列表计算，保持与加截断之前完全一致的结果
        if klines and anns_all:
            _di = {k[0]: i for i, k in enumerate(klines)}
            _idx = -1
            for _a in anns_all:
                if _a["date"] in _di:
                    _idx = _di[_a["date"]]; break
            if _idx >= 0:
                _base = klines[_idx-1][2] if _idx > 0 else klines[_idx][1]
                chg_ann = round((_c - _base) / _base * 100, 2) if _base else None
        # 安全阀：默认不限制（ANNO_MAX_PER_STOCK=0），只在显式配置时截断
        max_anns = announcement_limit()
        if max_anns and len(anns_all) > max_anns:
            anns = anns_all[-max_anns:]
        else:
            anns = anns_all
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

    # 拆分写数据：列表数据(文字+公告) 一次加载，K 线按代码分成 KLINE_SHARDS 个分片，
    # 前端首次选中某只股票时才加载对应分片（真正的按需加载，而不是注释里声称的）。
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
    try:
        _atomic_write_text(DASH / "data_list.js", js_list)
        manifest = write_kline_shards(DASH, kline_map)
    except Exception as e:
        raise RuntimeError(f"写入 data 文件失败（可能是文件被浏览器占用，或目录无写权限）：{e}") from e

    # GitHub Actions: 同时保存到 data_archive 以便下次恢复
    if is_github_actions():
        try:
            archive_dir = BASE / "data_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(DASH / "data_list.js", archive_dir / "data_list.js")
            for i in range(KLINE_SHARDS):
                src = DASH / f"data_kline_{i}.js"
                if src.exists():
                    shutil.copy2(src, archive_dir / src.name)
            shutil.copy2(DASH / "data_kline_manifest.js", archive_dir / "data_kline_manifest.js")
            # 归档里遗留的单文件产物会让下一轮恢复出 12MB 的陈旧副本，一并清掉。
            (archive_dir / "data_kline.js").unlink(missing_ok=True)
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
    print(
        f"  K线分片: {manifest['shards']} 片 / {manifest['codes']:,} 只 / 每只 {manifest['bars']} 根；"
        f"首屏只加载 data_list.js，选中股票时才拉对应分片"
    )
    print(f"  无K线数据: {[s['code'] for s in data if not s['klines']]}")

if __name__ == "__main__":
    main()
