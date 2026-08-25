"""规则化总结器（无token版）
读取 cninfo_announce_filtered.json + announce_txt/ 缓存原文，
按类别分组、正则提取关键数字（金额/比例/股数），
生成符合用户格式要求的 Markdown 报告。完全本地、不调用任何AI。
"""
import json, re
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root

BASE = data_root()
FILTERED = BASE / "cninfo_announce_filtered.json"
TXT_DIR = BASE / "announce_txt"
REPORTS = BASE / "reports"

# 报告章节顺序
SEC_ORDER = ["并购重组", "出售/转让资产", "人事变动", "质押/解押",
             "业绩快报/预告", "立案/处罚/重大诉讼", "分红/增持/回购"]
SEC_TITLE = {
    "并购重组": "💰 并购重组",
    "出售/转让资产": "📦 出售/转让资产",
    "人事变动": "👤 人事变动（董事长/总经理级别）",
    "质押/解押": "🔒 质押/解押（大额变动）",
    "业绩快报/预告": "📊 业绩快报/预告（极好/极差）",
    "立案/处罚/重大诉讼": "🔴 立案/处罚/重大诉讼",
    "分红/增持/回购": "💰 分红/增持/回购",
}
# cninfo分类标签 → 报告章节
CAT_TO_SEC = {
    "并购重组": "并购重组",
    "出售/转让": "出售/转让资产",
    "人事变动": "人事变动",
    "质押/解押": "质押/解押",
    "业绩预告": "业绩快报/预告",
    "立案/处罚": "立案/处罚/重大诉讼",
    "退市风险": "立案/处罚/重大诉讼",
    "破产重整": "立案/处罚/重大诉讼",
    "重大诉讼": "立案/处罚/重大诉讼",
    "分红/增持/回购": "分红/增持/回购",
}


def find_txt(code):
    cands = [p for p in TXT_DIR.glob(f"{code}_*.txt")]
    if not cands:
        return ""
    return max(cands, key=lambda p: p.stat().st_size).read_text(encoding="utf-8", errors="ignore")


def _num(s):
    return float(s.replace(",", "").replace("%", "").replace("亿元", "").replace("万元", "")
                .replace("元", "").replace("亿股", "").replace("万股", "").strip())


def extract_numbers(text):
    """从公告原文提取金额/比例/股数，并做轻量合理性过滤剔除OCR乱码。"""
    if not text:
        return []
    out, seen = [], set()
    pats = [
        r"\d[\d,]*(?:\.\d+)?\s*%",
        r"\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|元)",
        r"\d[\d,]*(?:\.\d+)?\s*(?:亿股|万股)",
    ]
    for p in pats:
        for m in re.findall(p, text):
            s = m.strip()
            try:
                v = _num(s)
            except Exception:
                continue
            # 过滤明显噪声：比例不可能 >1000%；金额/股数过大或过小视为乱码
            if "%" in s and (v < 0 or v > 1000):
                continue
            if "元" in s or "股" in s:
                if v <= 0 or v > 1e12:
                    continue
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out[:8]


def clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


# ============ 近半月相关股票概览（基于归档记忆）============
def build_universe(days=15, end_date=None):
    """合并近 days 天各次运行归档的筛选结果（去重），用于「近半月相关股票」展示。"""
    import datetime as _dt, re as _re
    ARCH = BASE / "cninfo_announce_archive"
    end = end_date or _dt.date.today().strftime("%Y-%m-%d")
    try:
        cutoff = (_dt.date.fromisoformat(end) - _dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = "2000-01-01"
    merged, seen = [], set()
    files = sorted(ARCH.glob("filtered_*.json")) if ARCH.exists() else []
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
    # 合并当期（可能尚未归档）
    if FILTERED.exists():
        try:
            for a in json.loads(FILTERED.read_text(encoding="utf-8")):
                k = (a.get("code"), a.get("title"), a.get("time"))
                if k in seen:
                    continue
                seen.add(k)
                merged.append(a)
        except Exception:
            pass
    return merged


def universe_summary(merged, days=15):
    """返回半月概览的 Markdown 行列表。"""
    L = []
    if not merged:
        L.append("**近半月无历史归档**（首次运行或归档为空）。可多次运行后查看近半月累计相关股票。")
        L.append("")
        return L
    stocks = {}
    for a in merged:
        code = a.get("code")
        sec = None
        for c in (a.get("cats") or []):
            if c in CAT_TO_SEC:
                sec = CAT_TO_SEC[c]
                break
        if sec is None:
            sec = "其他"
        stocks.setdefault(code, {"name": a.get("name", ""), "secs": set()})
        stocks[code]["secs"].add(sec)
    cat_count = defaultdict(set)
    for code, info in stocks.items():
        for s in info["secs"]:
            cat_count[s].add(code)
    L.append(f"> 合并近 {days} 天归档筛选结果（含当期，去重），共 **{len(stocks)}** 只相关股票，按类别分布如下：")
    L.append("")
    for sec in SEC_ORDER:
        codes = cat_count.get(sec, set())
        if not codes:
            continue
        sample = sorted(codes)[:12]
        names = "、".join(f"{stocks[c]['name']}({c})" for c in sample)
        more = f" 等{len(codes)}只" if len(codes) > 12 else ""
        L.append(f"- **{SEC_TITLE[sec].split(' ', 1)[-1]}**：{len(codes)} 只 — {names}{more}")
    L.append("")
    return L


HTML_TMPL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股公告总结（__RANGE__）</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#0f1420;color:#e6e9f0;padding:24px 16px 60px}
  .wrap{max-width:1240px;margin:0 auto}
  h1{font-size:20px;margin-bottom:6px}
  .sub{color:#8b93a7;font-size:13px;margin-bottom:16px;line-height:1.7}
  .filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  select,input{background:#0b101c;border:1px solid #2a3450;color:#e6e9f0;border-radius:8px;padding:8px 12px;font-size:13px;outline:none}
  select:focus,input:focus{border-color:#3b82f6}
  input{flex:1;min-width:200px}
  .cnt{color:#7a8298;font-size:12px;align-self:center}
  .tblwrap{overflow:auto;max-height:70vh;border:1px solid #232c40;border-radius:10px}
  table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;table-layout:fixed}
  th,td{border-bottom:1px solid #1d2436;padding:8px 10px;text-align:left;vertical-align:top}
  th{background:#171d2b;color:#aab2c5;font-weight:600;position:sticky;top:0;z-index:2;white-space:nowrap}
  tbody tr:hover{background:#151c2c}
  tbody tr:nth-child(even){background:#111827}
  tbody tr:nth-child(even):hover{background:#151c2c}
  a{color:#60a5fa;text-decoration:none;font-weight:600}
  a:hover{text-decoration:underline}
  .cd{color:#7a8298;font-weight:400;font-size:12px}
  .num{color:#f0abfc;word-break:break-all}
  .cat{color:#fbbf24;white-space:nowrap}
  .mv{color:#34d399;white-space:nowrap}
  .bd{color:#93c5fd;white-space:nowrap}
  .ttl{line-height:1.6;word-break:break-word}
  .pager{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:14px}
  .pager button{background:#1e3a8a;border:none;color:#fff;border-radius:8px;padding:8px 18px;font-size:13px;cursor:pointer}
  .pager button:disabled{opacity:.4;cursor:not-allowed}
  .pager .pi{color:#aab2c5;font-size:13px}
</style></head><body><div class="wrap">
<h1>📊 A股公告总结</h1>
<div class="sub">区间 <b>__RANGE__</b> · 共 <b id="totalN">__TOTAL__</b> 条 · 默认显示主板，可切换板块<br>
点击股票名可跳转到 K 线看板对应位置；下方可按板块、分类筛选，按关键词搜索。</div>
<div class="filters">
  <select id="boardSel">__BOARD_OPTIONS__</select>
  <select id="catSel">__CAT_OPTIONS__</select>
  <input id="kw" placeholder="搜索股票名 / 代码 / 公告标题">
  <span class="cnt" id="cnt"></span>
</div>
<div class="tblwrap">
<table>
<thead><tr><th style="width:150px">股票</th><th style="width:80px">市值</th><th style="width:90px">板块</th><th style="width:110px">分类</th><th style="width:100px">日期</th><th>公告标题</th><th style="width:170px">关键数字</th></tr></thead>
<tbody id="tbody"><tr><td colspan="7" style="text-align:center;padding:30px;color:#7a8298">加载中…</td></tr></tbody>
</table>
</div>
<div class="pager">
  <button id="prev">上一页</button>
  <span class="pi" id="pageInfo"></span>
  <button id="next">下一页</button>
</div>
</div>
<script>
const FILE = '__FILE__';
const SIZE = 50;
let page = 1, pages = 1;
let board = '主板', cat = '全部', q = '';
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function loadPage(){
  const url = '/api/report?file='+encodeURIComponent(FILE)+'&page='+page+'&size='+SIZE+'&board='+encodeURIComponent(board)+'&cat='+encodeURIComponent(cat)+'&q='+encodeURIComponent(q);
  document.getElementById('tbody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:30px;color:#7a8298">加载中…</td></tr>';
  fetch(url).then(function(r){return r.json();}).then(function(d){
    pages = d.pages || 1;
    document.getElementById('totalN').textContent = d.total;
    document.getElementById('cnt').textContent = '第 '+d.page+'/'+pages+' 页 · 共 '+d.total+' 条';
    document.getElementById('pageInfo').textContent = d.page + ' / ' + pages;
    document.getElementById('prev').disabled = d.page <= 1;
    document.getElementById('next').disabled = d.page >= pages;
    const rows = d.rows || [];
    document.getElementById('tbody').innerHTML = rows.map(function(r){
      return '<tr>'+
        '<td><a href="/reports/dashboard/dashboard.html?stock='+esc(r.code)+'">'+esc(r.name)+'<br><span class="cd">'+esc(r.code)+'</span></a></td>'+
        '<td class="mv">'+esc(r.mv)+'</td>'+
        '<td class="bd">'+esc(r.bd)+'</td>'+
        '<td class="cat">'+esc(r.cat_title)+'</td>'+
        '<td>'+esc(r.date)+'</td>'+
        '<td class="ttl">'+esc(r.title)+'</td>'+
        '<td class="num">'+esc(r.nums)+'</td></tr>';
    }).join('');
  }).catch(function(){ document.getElementById('tbody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:30px;color:#e6343a">加载失败，请重试</td></tr>'; });
}
document.getElementById('boardSel').addEventListener('change', function(){board=this.value;page=1;loadPage();});
document.getElementById('catSel').addEventListener('change', function(){cat=this.value;page=1;loadPage();});
document.getElementById('kw').addEventListener('input', function(){q=this.value.trim();page=1;loadPage();});
document.getElementById('prev').addEventListener('click', function(){if(page>1){page--;loadPage();}});
document.getElementById('next').addEventListener('click', function(){if(page<pages){page++;loadPage();}});
loadPage();
</script>
</body></html>"""


def fmt_mv(mv):
    if not mv or mv <= 0:
        return "—"
    if mv >= 1e12:
        return ("%.2f" % (mv / 1e12)) + "万亿"
    return ("%.1f" % (mv / 1e8)) + "亿"


def load_market_cap():
    p = REPORTS / "dashboard" / "data_list.js"
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        i = raw.index("[")
        j = raw.rindex("]") + 1
        arr = json.loads(raw[i:j])
        return {s.get("code"): s.get("market_cap", 0) for s in arr}
    except Exception:
        return {}


def build_report_html(start, end, now, rows, total, filename):
    import html as _h
    boards = []
    for r in rows:
        if r[4] not in boards:
            boards.append(r[4])
    board_opts = ('<option value="主板" selected>主板</option>' +
                  '<option value="全部">全部板块</option>' +
                  "".join('<option value="' + _h.escape(b) + '">' + _h.escape(b) + '</option>'
                          for b in boards if b not in ('主板', '全部')))
    cat_opts = '<option value="全部" selected>全部分类</option>' + "".join(
        '<option value="' + _h.escape(s) + '">' + _h.escape(SEC_TITLE[s]) + '</option>' for s in SEC_ORDER)
    html = HTML_TMPL
    html = html.replace("__RANGE__", start + " ~ " + end)
    html = html.replace("__TOTAL__", str(total))
    html = html.replace("__BOARD_OPTIONS__", board_opts)
    html = html.replace("__CAT_OPTIONS__", cat_opts)
    html = html.replace("__FILE__", filename)
    return html


def main(start, end):
    fl = build_universe(days=99999)  # 合并全部归档+当期，覆盖完整区间
    seen, items = set(), []
    for x in fl:
        k = (x.get("code"), x.get("title"))
        if k in seen:
            continue
        seen.add(k)
        items.append(x)

    # 市值映射
    mv_map = load_market_cap()

    # 按 (分类, 股票) 合并（含全部板块）
    groups = defaultdict(lambda: defaultdict(list))
    for x in items:
        sec = None
        for c in (x.get("cats") or []):
            if c in CAT_TO_SEC:
                sec = CAT_TO_SEC[c]
                break
        if sec is None:
            continue
        code = x.get("code", "")
        if code:
            groups[sec][code].append(x)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    total = 0
    for sec in SEC_ORDER:
        sec_codes = groups.get(sec, {})
        code_order = sorted(sec_codes.keys(),
                            key=lambda c: max((x.get("time", "") for x in sec_codes[c]), default=""),
                            reverse=True)
        for code in code_order:
            xs = sorted(sec_codes[code], key=lambda x: x.get("time", ""), reverse=True)
            name = xs[0].get("name", "")
            board = xs[0].get("board", "") or "主板"
            is_st = xs[0].get("is_st", False)
            dates = sorted({x.get("time", "")[:10] for x in xs if x.get("time", "")})
            date_str = dates[-1] if dates else "—"
            titles = [x.get("title", "") for x in xs]
            title_str = titles[0] if len(titles) == 1 else f"{titles[0]} 等{len(titles)}份公告"
            txt = find_txt(code)
            nums = list(dict.fromkeys(extract_numbers(txt)))[:10]
            nums_str = "；".join(nums) if nums else "—"
            mv = mv_map.get(code, 0)
            rows.append((sec, SEC_TITLE[sec], name, code, board, is_st, date_str, title_str, nums_str, mv))
            total += 1

    # 生成数据 JSON（全部 rows，供前端分页 API 按需加载）
    data_rows = []
    for sec, sec_title, name, code, board, is_st, date_str, title_str, nums_str, mv in rows:
        data_rows.append({
            "name": name, "code": code, "board": board, "is_st": is_st,
            "bd": board + ("·ST" if is_st else ""),
            "cat": sec, "cat_title": sec_title, "date": date_str,
            "title": title_str, "nums": nums_str, "mv": fmt_mv(mv),
            "kw": " ".join([name, code, title_str]),
        })
    data = {"range": start + " ~ " + end, "total": total, "rows": data_rows}
    REPORTS.mkdir(exist_ok=True)
    json_filename = f"A股公告总结_{start}_{end}.json"
    (REPORTS / json_filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    html = build_report_html(start, end, now, rows, total, json_filename)
    out_path = REPORTS / f"A股公告总结_{start}_{end}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"报告已生成：{out_path}（{total} 条）")
    return str(out_path)



if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else ""
    e = sys.argv[2] if len(sys.argv) > 2 else ""
    if not s or not e:
        print("用法: python rule_summarize.py <起始日期> <结束日期>  (YYYY-MM-DD)")
        sys.exit(1)
    main(s, e)
