"""方案A：巨潮资讯全量公告拉取 + 主板过滤 + 标题分析筛选
流程：全量拉标题 -> 过滤主板/排除ST -> 关键词筛选有价值类别 -> 输出清单
支持「记忆/增量」：第二次运行起只抓取上次结束日之后的公告，并把每期结果归档，
供「近半月相关股票」回顾使用。

GitHub Actions 适配：
- 状态文件和归档数据保存在仓库中（通过 gh-pages 分支持久化）
- 支持从 data_archive/ 目录恢复历史数据
"""
import json, re, time, urllib.request, urllib.parse, datetime, sys, os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 路径统一：打包(exe)后 __file__ 指向临时解压目录，需用 data_root() 定位可写数据目录(exe旁)
_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root, is_github_actions

BASE = data_root()
OUT = str(BASE / "cninfo_announce")
STATE_PATH = Path(OUT + "_state.json")
ARCHIVE_DIR = Path(OUT + "_archive")
# Colab 挂载 Google Drive 时，记忆会跨会话持久化（仅状态文件，归档仍在本地）
DRIVE_STATE = Path("/content/drive/MyDrive/mainboard_ann_state.json")

# GitHub Actions: 尝试从 data_archive 恢复历史数据
if is_github_actions():
    DATA_ARCHIVE = BASE / "data_archive"
    if DATA_ARCHIVE.exists():
        # 恢复状态文件
        if (DATA_ARCHIVE / "cninfo_announce_state.json").exists() and not STATE_PATH.exists():
            try:
                import shutil
                shutil.copy2(DATA_ARCHIVE / "cninfo_announce_state.json", STATE_PATH)
                print("[GA] 恢复状态文件从 data_archive")
            except Exception as e:
                print(f"[GA] 恢复状态文件失败: {e}")
        # 恢复归档目录
        if DATA_ARCHIVE.exists() and not ARCHIVE_DIR.exists():
            try:
                import shutil
                shutil.copytree(DATA_ARCHIVE, ARCHIVE_DIR)
                print("[GA] 恢复归档数据从 data_archive")
            except Exception as e:
                print(f"[GA] 恢复归档数据失败: {e}")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json",
}

def fetch_page(column, se_date, page, page_size=30):
    """拉取一页公告。对 403 限流做指数退避重试。"""
    data = {
        "pageNum": str(page),
        "pageSize": str(page_size),
        "column": column,
        "tabName": "fulltext",
        "plate": "",
        "stock": "",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    body = urllib.parse.urlencode(data).encode("utf-8")
    last_err = None
    for scheme in ("https", "http"):
        for attempt in range(3):
            try:
                url = f"{scheme}://www.cninfo.com.cn/new/hisAnnouncement/query"
                req = urllib.request.Request(url, data=body, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 403:
                    time.sleep(2.0 * (attempt + 1))  # 限流退避
                    continue
                break
            except Exception as e:
                last_err = e
                time.sleep(0.3)
    raise last_err

def fetch_range(column, se_date, workers=4):
    """拉取单个日期区间的全部页（用于单日等较小区间）。
    首页先拿 total，其余页并发拉取；巨潮无视 pageSize 参数（固定每页 30 条），
    串行翻页 + sleep 才是慢的根因，改为并发大幅提速。
    workers 控制在 4 左右，过高会触发巨潮对 IP 的限流封禁（403）。

    关键修复：并发抓取中失败的页不能静默丢弃（之前 return [] 导致整页 30 条
    公告凭空消失），改为收集失败页码，最后串行逐页重试补抓，确保完整。
    """
    all_items = []
    first = fetch_page(column, se_date, 1)
    total = first.get("totalAnnouncement", 0)
    all_items.extend(first.get("announcements") or [])
    total_pages = (total + 29) // 30
    if total_pages <= 1:
        return all_items, total

    failed_pages = []

    def work(p):
        try:
            res = fetch_page(column, se_date, p)
            items = res.get("announcements") or []
            if not items:
                failed_pages.append(p)
            return items
        except Exception:
            failed_pages.append(p)
            return []   # 失败页记入 failed_pages，稍后串行重试

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, p) for p in range(2, total_pages + 1)]
        for f in as_completed(futs):
            all_items.extend(f.result())

    # 串行重试失败页（并发 403 限流时，串行 + 退避通常能成功）
    if failed_pages:
        print(f"    [重试] {len(failed_pages)} 个失败页，串行补抓...")
        still_failed = []
        for p in sorted(failed_pages):
            try:
                res = fetch_page(column, se_date, p)
                items = res.get("announcements") or []
                all_items.extend(items)
                time.sleep(0.4)
            except Exception:
                still_failed.append(p)
        if still_failed:
            print(f"    [警告] 仍有 {len(still_failed)} 页抓取失败: {still_failed[:10]}")
    return all_items, total

def fetch_all(column, se_date):
    """拉取全部页。

    关键修复：巨潮 hisAnnouncement/query 对宽日期区间（多日）存在分页截断——
    返回按时间倒序，区间首日（如 8/15）的公告落在极深页码，服务端对深分页
    返回不稳定/被截断，导致首日公告丢失。改为「按天拆分」逐日拉取，单日区间
    页码可控（≤ ~60 页），保证完整抓取。
    """
    try:
        s_str, e_str = se_date.split("~")
        s = datetime.date.fromisoformat(s_str)
        e = datetime.date.fromisoformat(e_str)
    except Exception:
        return fetch_range(column, se_date)
    seen = set()
    merged = []
    cur = s
    while cur <= e:
        day = cur.strftime("%Y-%m-%d")
        items, tot = fetch_range(column, f"{day}~{day}")
        added = 0
        for it in items:
            k = (it.get("secCode"),
                 it.get("announcementTitle") or it.get("shortTitle"),
                 it.get("announcementTime"))
            if k in seen:
                continue
            seen.add(k)
            merged.append(it)
            added += 1
        print(f"    {day}: 拉取 {len(items)} 条，去重后新增 {added} 条")
        cur += datetime.timedelta(days=1)
    return merged, len(merged)

def fmt_time(ts):
    try:
        # 巨潮时间戳为 UTC 毫秒；显式转北京时间(UTC+8)。
        # 之前用 fromtimestamp() 跟随服务器本地时区，GitHub Actions 是 UTC，
        # 导致公告时间整体偏早 8 小时。
        dt = datetime.datetime.utcfromtimestamp(ts / 1000) + datetime.timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

def beijing_today():
    """返回北京时间当天日期。GitHub Actions 服务器为 UTC，需显式 +8 小时。
    之前多处用 datetime.date.today() 跟随服务器本地时区，UTC 环境下
    北京凌晨运行时会把「今天」算成前一天，导致增量窗口错一天。
    """
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()

# ============ 记忆 / 增量 ============
def load_state():
    for p in (DRIVE_STATE, STATE_PATH):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}

def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    # 若挂载了 Drive，跨会话持久化状态（只存状态文件，归档仍在本地）
    if DRIVE_STATE.parent.exists():
        try:
            DRIVE_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    # GitHub Actions: 同时保存到 data_archive 以便下次恢复
    if is_github_actions():
        try:
            archive_dir = BASE / "data_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            (archive_dir / "cninfo_announce_state.json").write_text(
                json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

def resolve_window(force_start=None, force_end=None, full_rescan=False):
    """返回 (START_DATE, END_DATE)。
    - force_start/force_end 给定 -> 强制使用（CLI 传参场景）
    - full_rescan -> 默认窗口 today-2..today
    - 否则 -> 增量：基于记忆 last_end_date（无记忆则用默认窗口）
    
    关键修复：如果上次结束日就是今天，说明今天已经运行过，
    此时应该只拉取今天的新公告（从当前时间往前推几小时），
    避免重复拉取全天数据。
    """
    today = beijing_today()
    end = force_end or today.strftime("%Y-%m-%d")
    if force_start:
        return force_start, end
    if full_rescan:
        start = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        return start, end
    st = load_state()
    last = st.get("last_end_date")
    if last:
        last_date = datetime.date.fromisoformat(last)
        # 如果上次就是今天，说明是今天内多次运行，只拉取今天数据
        if last_date == today:
            print(f"  今天({today})已运行过，将拉取今天的新增公告")
            return last, end  # 今天内增量，靠 seen 去重
        # 如果上次是昨天或更早，从上次结束日开始拉取
        return last, end
    start = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    return start, end

# 时间档位预设：名称 -> 回溯天数（含自然日，非交易日）
RANGE_PRESETS = {
    "3天": 3,
    "一周": 7,
    "半个月": 15,
    "一个月": 30,
}

def window_from_preset(preset_name):
    """根据档位名称（3天/一周/半个月/一个月）返回 (START, END)。"""
    days = RANGE_PRESETS.get(preset_name)
    if days is None:
        return None, None
    today = beijing_today()
    end = today
    start = today - datetime.timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def key_of(a):
    return (a.get("code"), a.get("title"), a.get("time"))

def archive_current(filtered, end_date):
    """归档当期筛选结果 + 更新记忆状态。
    
    关键修复：增量模式下，如果当天已经归档过，合并新旧数据而不是覆盖，
    确保一天内多次运行不会丢失数据。
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    arch = ARCHIVE_DIR / f"filtered_{end_date}.json"
    
    # 如果当天已有归档，合并数据（去重）
    existing = []
    if arch.exists():
        try:
            existing = json.loads(arch.read_text(encoding="utf-8"))
            print(f"  发现当天已有归档 {len(existing)} 条，将合并新数据")
        except Exception:
            pass
    
    # 合并并去重
    seen = set(key_of(a) for a in existing)
    merged = list(existing)  # 保留已有数据
    for a in filtered:
        k = key_of(a)
        if k not in seen:
            seen.add(k)
            merged.append(a)
    
    arch.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  归档完成：合并后共 {len(merged)} 条（新增 {len(merged) - len(existing)} 条）")
    
    st = load_state()
    st["last_end_date"] = end_date
    runs = st.setdefault("runs", [])
    # 更新或添加本次运行记录
    run_entry = {"date": end_date, "count": len(merged)}
    # 如果当天已有记录，更新它
    existing_run_idx = next((i for i, r in enumerate(runs) if r.get("date") == end_date), None)
    if existing_run_idx is not None:
        runs[existing_run_idx] = run_entry
    else:
        runs.append(run_entry)
    st["runs"] = runs[-90:]   # 保留最近 90 次，与看板数据范围一致
    save_state(st)
    
    # GitHub Actions: 同时复制到 data_archive
    if is_github_actions():
        try:
            archive_dir = BASE / "data_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(arch, archive_dir / arch.name)
        except Exception:
            pass

def build_universe(days=15, end_date=None):
    """合并近 days 天各次运行归档的筛选结果（去重），用于「近半月相关股票」展示。"""
    end = end_date or beijing_today().strftime("%Y-%m-%d")
    try:
        cutoff = (datetime.date.fromisoformat(end) - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = "2000-01-01"
    merged, seen = [], set()
    files = sorted(ARCHIVE_DIR.glob("filtered_*.json")) if ARCHIVE_DIR.exists() else []
    for fp in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", fp.name)
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
            k = key_of(a)
            if k in seen:
                continue
            seen.add(k)
            merged.append(a)
    # 合并当期（可能尚未归档）
    cur = Path(OUT + "_filtered.json")
    if cur.exists():
        try:
            for a in json.loads(cur.read_text(encoding="utf-8")):
                k = key_of(a)
                if k in seen:
                    continue
                seen.add(k)
                merged.append(a)
        except Exception:
            pass
    return merged

# ============ 板块识别 + 关键词筛选 ============
def board_of(code):
    """识别板块：主板/创业板/科创板/北交所/其他"""
    code = str(code or "").strip()
    if re.match(r"^(688|689)", code):
        return "科创板"
    if re.match(r"^30", code):
        return "创业板"
    if re.match(r"^(60|00)", code):
        return "主板"
    if re.match(r"^(83|87|88|92|43)", code):
        return "北交所"
    return "其他"

def is_st(name):
    return "ST" in str(name).upper()

def classify(title):
    KEYWORDS = {
        "并购重组": ["并购重组", "重大资产重组", "发行股份购买", "吸收合并", "收购", "取得控制", "控制权变更", "要约收购", "购买资产", "重组报告书", "重组预案"],
        "出售/转让": ["出售", "转让", "剥离", "挂牌", "清仓", "股权转让", "资产处置"],
        "人事变动": ["董事长", "总经理", "辞职", "辞任", "离任", "更换", "聘任", "董事长变更", "总经理变更", "独立董事辞职"],
        "质押/解押": ["质押", "解押", "解除质押", "再质押"],
        "业绩预告": ["业绩预告", "业绩快报", "预增", "预减", "预亏", "扭亏", "业绩变脸", "大幅增长", "大幅下降"],
        "立案/处罚": ["立案", "调查", "处罚", "警示函", "监管函", "处分", "行政处罚", "违规"],
        "退市风险": ["退市", "终止上市", "风险警示", "暂停上市", "摘牌"],
        "分红/增持/回购": ["分红", "派现", "送转", "增持", "回购", "利润分配"],
        "重大诉讼": ["诉讼", "仲裁", "起诉", "被诉", "判决"],
        "破产重整": ["重整", "破产", "债务重组", "预重整"],
    }
    matched = []
    for cat, kws in KEYWORDS.items():
        if any(kw in title for kw in kws):
            matched.append(cat)
    return matched

def is_noise(a):
    t = a["title"]
    if "股东大会议事规则" in t or "章程" in t:
        return True
    return False

def run_fetch(start=None, end=None, full_rescan=False):
    """执行一次拉取+过滤+归档+记忆。start/end 为 None 时按记忆/增量解析。
    返回 (filtered_delta, start_used, end_used)。
    """
    if start is None or end is None:
        start, end = resolve_window(start, end, full_rescan)
    se_date = f"{start}~{end}"
    print(f"拉取日期范围: {se_date}"
          + ("  [增量/记忆模式]" if (not full_rescan) else "  [全量重扫]"))

    # 增量模式下去重基础：已处理过的公告
    st = load_state()
    seen_set = set(tuple(x) for x in st.get("seen", [])) if (not full_rescan) else set()

    all_ann = []
    # 沪深两所串行拉取（每所内部按天、按页并发）。
    # 若两所同时并发会叠加请求量，易触发巨潮 IP 限流(403)，故串行更稳妥。
    for column in ("sse", "szse"):
        items, total = fetch_all(column, se_date)
        label = "上交所" if column == "sse" else "深交所"
        print(f"\n>>> 拉取{label} ({column})... 共 {total} 条，实际获取 {len(items)} 条")
        all_ann.extend(items)

    print(f"\n总计拉取公告: {len(all_ann)} 条")
    if not all_ann:
        print("⚠️ 警告：未拉取到任何公告。可能原因：当前网络无法访问巨潮 / 巨潮对云环境IP限流 / 所选日期区间内无交易日。")
        print("   建议：确认已联网；若仍持续为0，请在可正常访问巨潮的环境运行，或缩小日期区间后重试。")

    # 全板块保留：不再丢弃非主板/ST，而是给每条公告打上 board + is_st 标记。
    # 默认展示（报告/看板）仍只显示主板非ST，其他板块/ST 通过 UI 切换查看。
    all_announced = []
    for a in all_ann:
        code = str(a.get("secCode", "") or "").strip()
        name = str(a.get("secName", "") or "")
        title = a.get("shortTitle") or a.get("announcementTitle") or ""
        all_announced.append({
            "code": code,
            "name": name,
            "title": title,
            "time": fmt_time(a.get("announcementTime")),
            "url": a.get("adjunctUrl"),
            "pageColumn": a.get("pageColumn"),
            "board": board_of(code),
            "is_st": is_st(name),
        })

    # 统计各板块/ST 分布
    from collections import Counter
    board_cnt = Counter(x["board"] for x in all_announced)
    st_cnt = sum(1 for x in all_announced if x["is_st"])
    print(f"全板块公告: {len(all_announced)} 条 (含ST {st_cnt} 条)")
    print(f"  板块分布: " + " | ".join(f"{b} {c}" for b, c in board_cnt.most_common()))

    # 筛选有价值公告（全板块）：命中关键词的公告才进入 filtered，带 board/is_st 标记
    filtered = []
    for a in all_announced:
        matched = classify(a["title"])
        if matched:
            filtered.append({
                "code": a["code"],
                "name": a["name"],
                "title": a["title"],
                "time": a["time"],
                "url": a["url"],
                "pageColumn": a["pageColumn"],
                "board": a["board"],
                "is_st": a["is_st"],
                "cats": matched,
            })
    filtered = [a for a in filtered if not is_noise(a)]

    # 默认视图统计（主板非ST）
    default_view = [a for a in filtered if a["board"] == "主板" and not a["is_st"]]
    print(f"有价值公告(全板块): {len(filtered)} 条，其中默认视图(主板非ST): {len(default_view)} 条")

    # 增量去重：剔除已处理公告
    if seen_set:
        before = len(filtered)
        filtered = [a for a in filtered if key_of(a) not in seen_set]
        print(f"增量去重：剔除已处理 {before - len(filtered)} 条，本期新增 {len(filtered)} 条")

    if not filtered:
        print("\n📭 本期无新增公告（上次运行之后没有新的重点公告）。")
        with open(OUT + "_filtered.json", "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
        with open(OUT + "_all.json", "w", encoding="utf-8") as f:
            json.dump(all_announced, f, ensure_ascii=False)
        archive_current([], end)
        return [], start, end

    # 按类别分组输出（默认视图：主板非ST）
    print(f"\n筛选出有价值公告: {len(filtered)} 条（全板块）")
    print("=" * 90)
    cat_order = ["并购重组", "出售/转让", "人事变动", "质押/解押", "业绩预告", "立案/处罚", "退市风险", "破产重整", "分红/增持/回购", "重大诉讼"]
    for cat in cat_order:
        items = [a for a in filtered if cat in a["cats"]]
        if not items:
            continue
        print(f"\n### {cat} ({len(items)}条)")
        for a in sorted(items, key=lambda x: x["time"], reverse=True):
            tag = a["board"] + ("·ST" if a["is_st"] else "")
            print(f"  {a['time']} | {a['name']}({a['code']}) [{tag}] | {a['title'][:55]}")
            print(f"      url: {a['url']}")

    # 保存全板块公告 + 筛选结果（字段含 board/is_st）
    with open(OUT + "_all.json", "w", encoding="utf-8") as f:
        json.dump(all_announced, f, ensure_ascii=False, indent=2)
    with open(OUT + "_filtered.json", "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    print("\n\n已保存: _all.json (全板块全部公告), _filtered.json (全板块筛选结果)")

    # 归档 + 记忆（保存分析结果，供下次增量与半月回顾）
    archive_current(filtered, end)
    # 更新 seen（仅保留近 90 天，与看板数据范围一致）
    new_seen = list({key_of(a) for a in build_universe(days=90)}) if ARCHIVE_DIR.exists() else []
    st2 = load_state()
    st2["seen"] = [list(k) for k in new_seen]
    save_state(st2)

    return filtered, start, end

def main():
    force_start = sys.argv[1] if len(sys.argv) >= 3 else None
    force_end = sys.argv[2] if len(sys.argv) >= 3 else None
    full = "--full" in sys.argv
    run_fetch(force_start, force_end, full)

if __name__ == "__main__":
    main()

