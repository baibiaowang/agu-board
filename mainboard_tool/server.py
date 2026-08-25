"""本地网页界面（无token版）
用标准库 http.server 提供网页UI：选日期 → 点「生成报告」→ 实时日志 → 打开结果与看板。
双击「启动网页版.bat」即开（脚本会自动打开浏览器）。完全本地，不调用任何AI。
"""
import http.server, socketserver, json, threading, base64, secrets
import collections
from pathlib import Path
from datetime import date, timedelta
import run_full
import sys, os, re, html as _html, time, urllib.parse, gzip

# 路径统一：脚本目录用 resource_root（打包后 = _MEIPASS/scripts），数据目录用 data_root
_TOOL = Path(__file__).resolve().parent
_PROJ = _TOOL.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root

sys.path.insert(0, str(resource_root() / "scripts"))
import cninfo_fetch

BASE = data_root()
REPORTS = BASE / "reports"
# 服务器部署：HOST/PORT 支持环境变量覆盖，默认监听所有网卡（公网可访问）。
#   本地单机可 export HOST=127.0.0.1 仅本机访问。
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
# 访问密码：设置环境变量 AUTH_PASSWORD 后启用认证（HTTP Basic Auth + 可选 ?token= 参数）。
#   留空 = 无认证（本地桌面版默认）。服务器公网部署务必设置。
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

LOG = []
LOG_LOCK = threading.Lock()
DONE = False
RUNNING = False
RESULT = None

_GZIP_CACHE = collections.OrderedDict()
_GZIP_LOCK = threading.Lock()
_REPORT_CACHE = {}
_REPORT_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        LOG.append(msg)


def get_log():
    with LOG_LOCK:
        return list(LOG)


def do_run(start, end, preset=None, full_rescan=False):
    global DONE, RESULT, RUNNING
    RUNNING = True
    DONE = False
    try:
        res = run_full.run(start, end, log=log, preset=preset, full_rescan=full_rescan)
        RESULT = res
    except Exception as e:
        log("!! 异常: " + str(e))
        RESULT = None
    finally:
        DONE = True
        RUNNING = False


def default_window():
    # 走记忆/增量：有上次记录则从上次结束日续抓
    return run_full.default_window()


RANGE_OPTIONS = ["3天", "一周", "半个月", "一个月"]


def range_buttons_html():
    btns = "".join(
        f'<button class="rbtn" data-range="{r}">{r}</button>' for r in RANGE_OPTIONS
    )
    return btns


def _md_inline(s):
    """轻量 markdown 行内元素：加粗 / 链接（先转义再替换，零依赖）。"""
    s = _html.escape(s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2" target="_blank">\1</a>', s)
    s = re.sub(r'`(.+?)`', r'<code>\1</code>', s)
    return s


def md_to_html(md):
    """轻量 Markdown → HTML（仅报告用到的语法：标题/引用/分隔线/列表/加粗/链接），零第三方依赖。"""
    lines = md.splitlines()
    out, in_list, in_quote = [], None, False
    for line in lines:
        stripped = line.strip()
        # 引用块
        if stripped.startswith(">"):
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append(_md_inline(stripped[1:].strip()))
            continue
        if in_quote:
            out.append("</blockquote>")
            in_quote = False
        # 分隔线
        if stripped in ("---", "***", "___"):
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            out.append("<hr>")
            continue
        # 标题
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_md_inline(m.group(2))}</h{lvl}>")
            continue
        # 有序列表
        m = re.match(r'^(\d+)\.\s+(.*)$', stripped)
        if m:
            if in_list != "ol":
                if in_list:
                    out.append(f"</{in_list}>")
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{_md_inline(m.group(2))}</li>")
            continue
        # 无序列表
        m = re.match(r'^[-*+]\s+(.*)$', stripped)
        if m:
            if in_list != "ul":
                if in_list:
                    out.append(f"</{in_list}>")
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            continue
        # 空行
        if not stripped:
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            continue
        # 普通段落
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        out.append(f"<p>{_md_inline(stripped)}</p>")
    if in_list:
        out.append(f"</{in_list}>")
    if in_quote:
        out.append("</blockquote>")
    return "\n".join(out)


def _fmt_size(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def list_reports():
    """扫描 reports/ 下所有 A股主板公告总结_*.md / *.html，按生成时间倒序返回。"""
    items = []
    if REPORTS.exists():
        for p in list(REPORTS.glob("A股公告总结_*")) + list(REPORTS.glob("A股主板公告总结_*")):
            name = p.name
            if not (name.endswith(".md") or name.endswith(".html")):
                continue
            stem = name[:-5] if name.endswith(".html") else name[:-3]
            parts = stem.split("_")
            if len(parts) < 3:
                continue
            items.append({
                "file": name,
                "start": parts[-2],
                "end": parts[-1],
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def history_html():
    """首页「历史数据」区：列出现有报告 + 看板入口，打开即看、无需重拉。"""
    reports = list_reports()
    if not reports:
        return '<div class="tag">暂无历史数据，点击上方「生成报告」生成第一份。</div>'
    lis = []
    for it in reports[:15]:  # 最多展示最近 15 份
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(it["mtime"]))
        lis.append(
            f'<li><a class="hist-a" href="/reports/{it["file"]}" target="_blank">'
            f'{_html.escape(it["file"])}</a>'
            f'<span class="tag">{it["start"]} ~ {it["end"]} · {_fmt_size(it["size"])} · {t}</span></li>'
        )
    return (
        '<div style="margin-bottom:10px">'
        '<a class="dash-a" href="/reports/dashboard/dashboard.html" target="_blank">📈 打开 K 线看板（最新）</a></div>'
        '<div style="font-size:13px;color:#aab2c5;margin:10px 0 6px">历史总结报告（倒序，点击打开）：</div>'
        '<ul class="hist-list">' + "".join(lis) + "</ul>"
    )


def get_recent_stocks(n=80):
    """读取看板数据 data_list.js，返回主板最近新增股票（按最新公告发布日期倒序，取前 n 只），附 latest_date。"""
    p = REPORTS / "dashboard" / "data_list.js"
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
        i2 = raw.index("[")
        j2 = raw.rindex("]") + 1
        arr = json.loads(raw[i2:j2])
    except Exception:
        return []
    def latest(s):
        ds = [a.get("date", "") for a in s.get("announcements", []) if a.get("date")]
        return max(ds) if ds else ""
    # 默认只展示主板
    arr = [s for s in arr if (s.get("board") or "主板") == "主板"]
    for s in arr:
        s["latest_date"] = latest(s)
    arr.sort(key=lambda s: s.get("latest_date", ""), reverse=True)
    return arr[:n]


def stock_card(s):
    code = s.get("code", "")
    name = s.get("name", "")
    chg = s.get("chg") or 0
    cat = s.get("category", "")
    cls = "up" if chg >= 0 else "down"
    sign = "+" if chg >= 0 else ""
    l = s.get("latest_date", "")
    days_txt = l[5:] if l else ""
    return ('<a class="stock-card" href="/reports/dashboard/dashboard.html?stock=' + code + '" target="_blank" data-cat="' + _html.escape(cat) + '">'
            '<div class="nm">' + _html.escape(name) + '</div>'
            '<div class="cd">' + _html.escape(code) + '</div>'
            '<div class="chg ' + cls + '">' + sign + ('%.2f' % chg) + '%</div>'
            '<div class="cat">' + _html.escape(cat) + '</div>'
            '<div class="days">' + days_txt + '</div></a>')


def latest_report_link():
    reps = list_reports()
    return "/reports/" + reps[0]["file"] if reps else "#"


def build_html(start, end):
    stocks = get_recent_stocks(80)
    cats = []
    for s in stocks:
        c = s.get("category", "其他")
        if c not in cats:
            cats.append(c)
    cat_tabs = ('<span class="cat-tab active" data-cat="全部">全部</span>' +
                "".join('<span class="cat-tab" data-cat="' + _html.escape(c) + '">' + _html.escape(c) + '</span>' for c in cats))
    stock_html = "".join(stock_card(s) for s in stocks)
    report_link = latest_report_link()
    return r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股公告看板</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       background:#0a0e18;color:#e6e9f0;min-height:100vh;line-height:1.6;
       background-image:radial-gradient(1200px 500px at 15% -10%,rgba(59,130,246,.14),transparent 60%),
                        radial-gradient(900px 420px at 90% 0%,rgba(139,92,246,.12),transparent 55%)}
  .wrap{max-width:920px;margin:0 auto;padding:40px 20px 64px}
  .header{text-align:center;margin-bottom:26px}
  .header h1{font-size:26px;font-weight:800;letter-spacing:.5px;color:#f0f4ff}
  .header .sub{color:#8b93a7;font-size:13px;margin-top:8px}
  .clock{color:#8b93a7;font-size:13px;margin-top:6px;font-variant-numeric:tabular-nums}
  .actions{display:flex;justify-content:center;align-items:center;gap:24px;margin-bottom:30px;flex-wrap:wrap}
  .btn-update{position:relative;overflow:hidden;width:124px;height:124px;border-radius:50%;background:linear-gradient(135deg,#3b82f6,#8b5cf6);
    color:#fff;font-size:22px;font-weight:700;border:none;cursor:pointer;transition:.18s;
    box-shadow:0 8px 26px rgba(59,130,246,.42);display:flex;align-items:center;justify-content:center;letter-spacing:2px}
  .btn-update:hover{transform:translateY(-2px) scale(1.04);box-shadow:0 12px 30px rgba(59,130,246,.55)}
  .btn-update:disabled{opacity:.85;cursor:not-allowed;transform:none}
  .btn-update .fill{position:absolute;left:0;bottom:0;width:100%;height:0%;background:rgba(255,255,255,.30);transition:height .6s cubic-bezier(.4,0,.2,1)}
  .btn-update .btn-txt{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
  .btn-update .btn-label{font-size:22px;font-weight:700;letter-spacing:2px;line-height:1}
  .btn-update .btn-log{font-size:9px;margin-top:4px;opacity:.95;text-align:center;line-height:1.3;max-width:108px}
  .btn-update.done{animation:glow 1.6s ease-in-out infinite}
  @keyframes glow{0%,100%{box-shadow:0 8px 26px rgba(59,130,246,.42)}50%{box-shadow:0 0 46px rgba(99,102,241,.95)}}
  .btn-side{display:flex;flex-direction:column;gap:12px}
  .btn-side a,.btn-side button{display:block;text-align:center;padding:13px 24px;border-radius:12px;text-decoration:none;
    font-size:15px;font-weight:600;transition:.15s;min-width:140px}
  .btn-side button{border:none;cursor:pointer;font-family:inherit}
  .btn-dash{background:rgba(52,211,153,.10);color:#34d399;border:1px solid rgba(52,211,153,.28)}
  .btn-dash:hover{background:rgba(52,211,153,.20)}
  .btn-report{background:rgba(251,191,36,.10);color:#fbbf24;border:1px solid rgba(251,191,36,.28)}
  .btn-report:hover{background:rgba(251,191,36,.20)}
  .btn-reset{background:rgba(239,68,68,.10);color:#f87171;border:1px solid rgba(239,68,68,.28)}
  .btn-reset:hover{background:rgba(239,68,68,.20)}
  .card{background:#121826;border:1px solid #232c40;border-radius:14px;padding:20px 22px;margin-bottom:20px;box-shadow:0 6px 24px rgba(0,0,0,.28)}
  .card-title{font-weight:700;font-size:15px;color:#e6e9f0;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .cnt{font-size:12px;color:#7a8298;font-weight:400}
  .cat-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
  .cat-tab{padding:6px 14px;border-radius:999px;border:1px solid #2a3448;background:#0b101c;color:#8b93a7;
    font-size:13px;cursor:pointer;transition:.15s;user-select:none}
  .cat-tab:hover{border-color:#3b82f6;color:#aab2c5}
  .cat-tab.active{background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;border-color:transparent}
  .stocks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .stock-card{background:#0b101c;border:1px solid #1d2436;border-radius:10px;padding:11px 13px;text-decoration:none;transition:.15s;display:block}
  .stock-card:hover{border-color:#3b82f6;transform:translateY(-2px);background:#121a2b}
  .stock-card .nm{color:#e6e9f0;font-size:14px;font-weight:600}
  .stock-card .cd{color:#7a8298;font-size:12px;margin-top:1px}
  .stock-card .chg{font-size:14px;font-weight:700;margin-top:6px}
  .up{color:#e6343a}.down{color:#0aa858}
  .stock-card .cat{color:#fbbf24;font-size:11px;margin-top:3px}
  .stock-card .days{color:#8b93a7;font-size:11px;margin-top:2px}
  @media (max-width:640px){
    .wrap{padding:28px 14px 48px}
    .header h1{font-size:22px}
    .actions{gap:16px}
    .btn-update{width:100px;height:100px}
    .btn-update .btn-label{font-size:18px}
    .stocks-grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr))}
  }
</style></head>
<body><div class="wrap">
  <div class="header">
    <h1>A股公告看板</h1>
    <div class="sub">巨潮全量公告 · 规则引擎总结 · K线看板</div>
    <div class="clock" id="clock"></div>
  </div>

  <div class="actions">
    <button id="go" class="btn-update" onclick="startRun()">
      <span class="fill" id="fill"></span>
      <span class="btn-txt"><span class="btn-label">更新</span><span class="btn-log" id="btnLog"></span></span>
    </button>
    <div class="btn-side">
      <a class="btn-dash" href="/reports/dashboard/dashboard.html" target="_blank">打开K线看板</a>
      <a class="btn-report" href="__REPORT__" target="_blank">看公告</a>
      <button class="btn-reset" onclick="startRun(true)">重置（近3月）</button>
    </div>
  </div>

  <div class="card">
    <div class="card-title">最近新增股票 <span class="cnt" id="scnt"></span></div>
    <div class="cat-tabs">
__CAT_TABS__
    </div>
    <div class="stocks-grid">
__STOCKS__
    </div>
  </div>
</div>

<script>
let timer=null;
function tick(){const d=new Date();const wd=['日','一','二','三','四','五','六'][d.getDay()];const p=n=>String(n).padStart(2,'0');document.getElementById('clock').textContent=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' 周'+wd+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());}tick();setInterval(tick,1000);
function filterStocks(){
  const c=document.querySelector('.cat-tab.active').dataset.cat;
  let n=0;
  document.querySelectorAll('.stock-card').forEach(function(card){
    const show=(c==='全部'||card.dataset.cat===c);
    card.style.display=show?'':'none';
    if(show)n++;
  });
  document.getElementById('scnt').textContent=n+' 只';
}
document.querySelectorAll('.cat-tab').forEach(function(t){
  t.onclick=function(){
    document.querySelectorAll('.cat-tab').forEach(function(x){x.classList.toggle('active',x===t);});
    filterStocks();
  };
});
const STEPS=['拉取公告','抽取原文','生成报告','更新看板'];
function stepFromLog(log){let s=0;for(const line of (log||[])){const m=line.match(/\[(\d)\/4\]/);if(m)s=Math.max(s,parseInt(m[1]));}return s;}
function startRun(full){
  const btn=document.getElementById('go');
  btn.disabled=true;
  document.getElementById('btnLog').textContent='启动中…';
  document.getElementById('fill').style.height='4%';
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start:'',end:'',full_rescan:!!full})}).then(r=>r.json());
  if(timer) clearInterval(timer);
  timer=setInterval(poll,1200);
}
function poll(){
  fetch('/api/log').then(r=>r.json()).then(d=>{
    const step=stepFromLog(d.log);
    const btn=document.getElementById('go');
    const bl=document.getElementById('btnLog');
    const fill=document.getElementById('fill');
    if(d.done){
      fill.style.height='100%';
      bl.textContent='已完成 ✓';
      btn.classList.add('done');
      btn.disabled=false;
      clearInterval(timer);
      setTimeout(function(){location.reload();},1400);
    }else if(step>0){
      fill.style.height=(step*25)+'%';
      bl.textContent=step+'/4 '+STEPS[step-1];
    }else{
      fill.style.height='4%';
      bl.textContent='启动中…';
    }
  }).catch(function(){});
}
</script></body></html>""".replace("__CAT_TABS__", cat_tabs).replace("__STOCKS__", stock_html).replace("__REPORT__", report_link)


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _authed(self):
        """认证检查：未设置 AUTH_PASSWORD 则放行；否则校验 Basic Auth 或 ?token= 参数。"""
        if not AUTH_PASSWORD:
            return True
        # 1) HTTP Basic Auth（浏览器自动弹登录框，输对后自动带上凭据）
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                pw = decoded.partition(":")[2]
                if secrets.compare_digest(pw, AUTH_PASSWORD):
                    return True
            except Exception:
                pass
        # 2) ?token= 参数（方便分享看板/报告链接，如 /reports/dashboard/dashboard.html?token=xxx）
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "token" in q and secrets.compare_digest(q["token"][0], AUTH_PASSWORD):
            return True
        return False

    def _send_auth(self):
        """返回 401 + WWW-Authenticate，浏览器会弹登录框。"""
        body = ("<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>需要登录</title></head><body style=\"margin:0;font-family:-apple-system,'Segoe UI',sans-serif;"
                "background:#0f1420;color:#e6e9f0;display:flex;align-items:center;justify-content:center;height:100vh\">"
                "<div style=\"text-align:center\"><div style=\"font-size:42px\">🔒</div>"
                "<h2 style=\"margin:12px 0 6px\">需要登录</h2>"
                "<p style=\"color:#8b93a7;font-size:14px\">请输入访问密码（或访问链接带 ?token=密码）</p></div>"
                "</body></html>")
        data = body.encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AGu Board"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authed():
            self._send_auth()
            return
        # 剥离查询串（如 ?token=xxx），用纯路径匹配
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, build_html(*default_window()), "text/html")
        elif path.startswith("/api/log"):
            with LOG_LOCK:
                self._send(200, json.dumps({"log": list(LOG), "done": DONE,
                                            "running": RUNNING, "result": RESULT}, ensure_ascii=False))
        elif path.startswith("/api/report"):
            # 报告分页 API：/api/report?file=xxx.json&page=N&size=50&board=主板&cat=全部&q=关键词
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fname = q.get("file", [""])[0]
            try:
                page = int(q.get("page", ["1"])[0] or 1)
                size = int(q.get("size", ["50"])[0] or 50)
            except Exception:
                page, size = 1, 50
            size = min(max(size, 1), 200)
            board_f = q.get("board", [""])[0]
            cat_f = q.get("cat", [""])[0]
            qq = q.get("q", [""])[0].strip().lower()
            if not fname.endswith(".json"):
                self._send(400, json.dumps({"error": "bad file"}))
                return
            p = (REPORTS / fname).resolve()
            if not p.exists() or not str(p).startswith(str(REPORTS)):
                self._send(404, json.dumps({"error": "not found"}))
                return
            rkey = (str(p), p.stat().st_mtime)
            with _REPORT_LOCK:
                data = _REPORT_CACHE.get(rkey)
            if data is None:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    self._send(500, json.dumps({"error": "read fail"}))
                    return
                with _REPORT_LOCK:
                    _REPORT_CACHE[rkey] = data
                    if len(_REPORT_CACHE) > 8:
                        _REPORT_CACHE.clear()
            rows = data.get("rows", [])
            filtered = []
            for r in rows:
                if board_f and board_f != "全部" and r.get("board") != board_f:
                    continue
                if cat_f and cat_f != "全部" and r.get("cat") != cat_f:
                    continue
                if qq and qq not in (r.get("kw") or "").lower():
                    continue
                filtered.append(r)
            total = len(filtered)
            pages = max(1, (total + size - 1) // size)
            page = min(max(1, page), pages)
            start_idx = (page - 1) * size
            page_rows = filtered[start_idx:start_idx + size]
            self._send(200, json.dumps({"total": total, "page": page, "size": size,
                                        "pages": pages, "rows": page_rows}, ensure_ascii=False))
        elif path.startswith("/reports/"):
            rel = urllib.parse.unquote(path[len("/reports/"):])
            p = (REPORTS / rel).resolve()
            if p.exists() and str(p).startswith(str(REPORTS)):
                if p.suffix == ".md":
                    # 报告为 Markdown，浏览器无法直接渲染 → 转 HTML 展示
                    try:
                        md = p.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        md = ""
                    body = ("<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                            f"<title>{_html.escape(p.stem)}</title>"
                            "<style>"
                            "body{margin:0;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
                            "background:#0f1420;color:#e6e9f0;padding:32px 20px 60px}"
                            ".wrap{max-width:900px;margin:0 auto}"
                            "h1{font-size:24px;border-bottom:1px solid #232c40;padding-bottom:10px}"
                            "h2{font-size:20px}h3{font-size:17px;margin-top:28px;color:#fbbf24}"
                            "h4{font-size:15px}"
                            "blockquote{border-left:3px solid #3b82f6;margin:14px 0;padding:8px 14px;"
                            "background:#171d2b;color:#aab2c5;border-radius:6px}"
                            "hr{border:none;border-top:1px solid #232c40;margin:22px 0}"
                            "p{line-height:1.8;color:#c6cddc}li{line-height:1.8;color:#c6cddc;margin:6px 0}"
                            "a{color:#60a5fa}code{background:#0b0f18;padding:1px 5px;border-radius:4px;"
                            "font-family:Consolas,monospace;color:#f0abfc}"
                            "</style></head><body><div class=\"wrap\">" + md_to_html(md) + "</div></body></html>")
                    data = body.encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                    cache_ctl = "no-cache"
                else:
                    data = p.read_bytes()
                    if p.suffix == ".js":
                        ctype = "application/javascript; charset=utf-8"
                    elif p.suffix in (".html", ".htm"):
                        ctype = "text/html; charset=utf-8"
                    elif p.suffix == ".css":
                        ctype = "text/css; charset=utf-8"
                    else:
                        ctype = "application/octet-stream"
                    cache_ctl = "public, max-age=31536000" if "echarts" in p.name else "no-cache"
                # 缓存：Last-Modified 条件请求，未变化返回 304（免重新下载）
                mtime = p.stat().st_mtime
                last_mod = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(mtime))
                if self.headers.get("If-Modified-Since") == last_mod:
                    self.send_response(304)
                    self.send_header("Cache-Control", cache_ctl)
                    self.end_headers()
                    return
                # gzip 压缩传输（JSON/文本压缩率高，带宽受限时显著加速下载）
                gz = False
                if "gzip" in self.headers.get("Accept-Encoding", "") and len(data) > 1024:
                    gkey = (str(p), mtime, len(data))
                    with _GZIP_LOCK:
                        _hit = _GZIP_CACHE.get(gkey)
                        if _hit is not None:
                            _GZIP_CACHE.move_to_end(gkey)
                            data = _hit
                        else:
                            data = gzip.compress(data, compresslevel=6)
                            _GZIP_CACHE[gkey] = data
                            _GZIP_CACHE.move_to_end(gkey)
                            while len(_GZIP_CACHE) > 32:
                                _GZIP_CACHE.popitem(last=False)
                    gz = True
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Last-Modified", last_mod)
                self.send_header("Cache-Control", cache_ctl)
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, "not found")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if not self._authed():
            self._send_auth()
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/run":
            ln = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(ln) or b"{}")
            global RUNNING
            if RUNNING:
                self._send(200, json.dumps({"ok": False, "msg": "正在运行中"}))
                return
            preset = data.get("preset")
            full_rescan = bool(data.get("full_rescan") or data.get("reset"))
            if full_rescan:
                s, e = "", ""
            elif preset and preset in cninfo_fetch.RANGE_PRESETS:
                s, e = cninfo_fetch.window_from_preset(preset)
            else:
                s = data.get("start") or default_window()[0]
                e = data.get("end") or default_window()[1]
            with LOG_LOCK:
                LOG.clear()
            global DONE, RESULT
            DONE = False
            RESULT = None
            threading.Thread(target=do_run, args=(s, e, preset, full_rescan), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "start": s, "end": e}))
        else:
            self._send(404, "not found")

    def log_message(self, *a):
        pass


def main():
    s = default_window()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    # 绑定 HOST（默认 0.0.0.0 监听所有网卡，供服务器/局域网访问）
    server = socketserver.ThreadingTCPServer((HOST, PORT), H)
    print(f"主板公告工具已启动: http://{HOST}:{PORT}/  (默认区间 {s[0]}~{s[1]})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止")


if __name__ == "__main__":
    main()
