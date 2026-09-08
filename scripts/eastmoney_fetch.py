"""东方财富公告抓取器（兼容原 cninfo_fetch.py 输出结构）。"""
import datetime as dt, json, random, re, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path: sys.path.insert(0, str(_PROJ))
from path_util import data_root, is_github_actions
BASE=data_root(); OUT=BASE/"cninfo_announce"; STATE_PATH=Path(str(OUT)+"_state.json"); ARCHIVE_DIR=Path(str(OUT)+"_archive")
DRIVE_STATE=Path("/content/drive/MyDrive/mainboard_ann_state.json")
API_URL="https://np-anotice-stock.eastmoney.com/api/security/ann"
DETAIL_URL="https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36","Referer":"https://data.eastmoney.com/notices/","Accept":"application/json,text/plain,*/*","Connection":"close"}
RETRYABLE_HTTP={403,408,425,429,500,502,503,504}; PAGE_SIZE=100; MAX_RETRIES=5
def beijing_today(): return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
def load_state():
    for p in (DRIVE_STATE,STATE_PATH):
        try:
            if p.exists(): return json.loads(p.read_text(encoding="utf-8"))
        except Exception: pass
    return {}
def save_state(st):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True); text=json.dumps(st,ensure_ascii=False,indent=2); STATE_PATH.write_text(text,encoding="utf-8")
    if DRIVE_STATE.parent.exists():
        try: DRIVE_STATE.write_text(text,encoding="utf-8")
        except Exception: pass
    if is_github_actions():
        d=BASE/"data_archive"; d.mkdir(parents=True,exist_ok=True); (d/"cninfo_announce_state.json").write_text(text,encoding="utf-8")
def resolve_window(force_start=None,force_end=None,full_rescan=False):
    today=beijing_today(); end=force_end or today.strftime("%Y-%m-%d")
    if force_start:return force_start,end
    if full_rescan:return (today-dt.timedelta(days=2)).strftime("%Y-%m-%d"),end
    last=load_state().get("last_end_date")
    try:
        if last:return dt.date.fromisoformat(last).strftime("%Y-%m-%d"),end
    except Exception:pass
    return (today-dt.timedelta(days=2)).strftime("%Y-%m-%d"),end
RANGE_PRESETS={"3天":3,"一周":7,"半个月":15,"一个月":30}
def window_from_preset(name):
    days=RANGE_PRESETS.get(name)
    if not days:return None,None
    end=beijing_today(); start=end-dt.timedelta(days=days-1); return start.strftime("%Y-%m-%d"),end.strftime("%Y-%m-%d")
def _request_json(params):
    last_err=None; url=API_URL+"?"+urllib.parse.urlencode(params)
    for attempt in range(MAX_RETRIES):
        try:
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=25) as resp:
                obj=json.loads(resp.read().decode(resp.headers.get_content_charset() or "utf-8",errors="replace"))
                if not isinstance(obj,dict):raise ValueError("返回格式异常")
                return obj
        except urllib.error.HTTPError as e:
            last_err=e
            if e.code not in RETRYABLE_HTTP:break
        except Exception as e:last_err=e
        wait=min(30,1.5*(2**attempt))+random.uniform(0,0.8); print(f"    [东方财富重试] {last_err}，第 {attempt+1}/{MAX_RETRIES} 次，等待 {wait:.1f}s",flush=True); time.sleep(wait)
    raise RuntimeError(f"东方财富公告接口连续失败: {last_err}") from last_err
def _extract_data(obj):
    data=obj.get("data") or {}
    if isinstance(data,dict):
        rows=data.get("list") or []; return rows if isinstance(rows,list) else [],int(data.get("total_hits") or data.get("total") or 0)
    rows=obj.get("list") or []; return rows if isinstance(rows,list) else [],len(rows) if isinstance(rows,list) else 0
def fetch_page(begin,end,page):
    return _extract_data(_request_json({"sr":"-1","page_size":str(PAGE_SIZE),"page_index":str(page),"ann_type":"SHA,CYB,SZA,BJA","client_source":"web","f_node":"0","s_node":"0","begin_time":begin,"end_time":end}))
def _stock_info(item):
    codes=item.get("codes") or []
    if isinstance(codes,list) and codes and isinstance(codes[0],dict):
        c=codes[0]; return str(c.get("stock_code") or "").strip(),str(c.get("short_name") or c.get("stock_name") or "").strip()
    return str(item.get("stock_code") or item.get("sec_code") or "").strip(),str(item.get("short_name") or item.get("stock_name") or "").strip()
def fmt_time(item):
    raw=str(item.get("notice_date") or item.get("display_time") or item.get("eiTime") or item.get("sort_date") or "").strip().replace("T"," ")
    m=re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?$",raw)
    return f"{m.group(1)} {m.group(2) or '00:00'}" if m else raw[:19]
def board_of(code):
    code=str(code or "").strip()
    if re.match(r"^(688|689)",code):return "科创板"
    if re.match(r"^30",code):return "创业板"
    if re.match(r"^(60|00|001|002|003)",code):return "主板"
    if re.match(r"^(83|87|88|92|43)",code):return "北交所"
    return "其他"
def is_st(name):return "ST" in str(name).upper()
def classify(title):
    K={"并购重组":["并购重组","重大资产重组","发行股份购买","吸收合并","收购","取得控制","控制权变更","要约收购","购买资产","重组报告书","重组预案"],"出售/转让":["出售","转让","剥离","挂牌","清仓","股权转让","资产处置"],"人事变动":["董事长","总经理","辞职","辞任","离任","更换","聘任","董事长变更","总经理变更","独立董事辞职"],"质押/解押":["质押","解押","解除质押","再质押"],"业绩预告":["业绩预告","业绩快报","预增","预减","预亏","扭亏","业绩变脸","大幅增长","大幅下降"],"立案/处罚":["立案","调查","处罚","警示函","监管函","处分","行政处罚","违规"],"退市风险":["退市","终止上市","风险警示","暂停上市","摘牌"],"分红/增持/回购":["分红","派现","送转","增持","回购","利润分配"],"重大诉讼":["诉讼","仲裁","起诉","被诉","判决"],"破产重整":["重整","破产","债务重组","预重整"]}
    return [cat for cat,kws in K.items() if any(k in title for k in kws)]
def is_noise(a):return "股东大会议事规则" in a.get("title","") or "章程" in a.get("title","")
def key_of(a):return (a.get("code"),a.get("title"),a.get("time"))
def normalize(item):
    code,name=_stock_info(item); title=str(item.get("title_ch") or item.get("title") or item.get("announcement_title") or "").strip(); art=str(item.get("art_code") or item.get("artcode") or "").strip()
    cols=item.get("columns") or []; pc=str(cols[0].get("column_code") or cols[0].get("column_name") or "") if isinstance(cols,list) and cols and isinstance(cols[0],dict) else ""
    return {"code":code,"name":name,"title":title,"time":fmt_time(item),"url":DETAIL_URL.format(code=code,art_code=art) if code and art else "","pageColumn":pc,"board":board_of(code),"is_st":is_st(name)}
def fetch_all_days(start,end):
    cur=dt.date.fromisoformat(start); last=dt.date.fromisoformat(end); out=[]
    while cur<=last:
        day=cur.strftime("%Y-%m-%d"); page=1; rows_day=[]; total=None; print(f"  [东方财富] 抓取 {day} ...",flush=True)
        while True:
            rows,total=fetch_page(day,day,page); rows_day.extend(rows)
            if not rows or len(rows)<PAGE_SIZE or (total and page*PAGE_SIZE>=total):break
            page+=1
        print(f"    {day}: 获取 {len(rows_day)} 条",flush=True); out.extend(rows_day); cur+=dt.timedelta(days=1)
    return out
def archive_current(filtered,end_date):
    ARCHIVE_DIR.mkdir(parents=True,exist_ok=True); arch=ARCHIVE_DIR/f"filtered_{end_date}.json"; old=[]
    try:
        if arch.exists():old=json.loads(arch.read_text(encoding="utf-8"))
    except Exception:pass
    seen={key_of(x) for x in old}; merged=list(old)
    for x in filtered:
        if key_of(x) not in seen:seen.add(key_of(x)); merged.append(x)
    arch.write_text(json.dumps(merged,ensure_ascii=False,indent=2),encoding="utf-8")
    st=load_state(); st["last_end_date"]=end_date; runs=st.setdefault("runs",[]); entry={"date":end_date,"count":len(merged)}; idx=next((i for i,r in enumerate(runs) if r.get("date")==end_date),None)
    if idx is None:runs.append(entry)
    else:runs[idx]=entry
    st["runs"]=runs[-90:]; save_state(st)
    if is_github_actions():
        d=BASE/"data_archive"; d.mkdir(parents=True,exist_ok=True); (d/arch.name).write_text(arch.read_text(encoding="utf-8"),encoding="utf-8")
def build_universe(days=90,end_date=None):
    end=end_date or beijing_today().strftime("%Y-%m-%d"); cutoff=(dt.date.fromisoformat(end)-dt.timedelta(days=days-1)).strftime("%Y-%m-%d"); merged=[]; seen=set()
    for fp in sorted(ARCHIVE_DIR.glob("filtered_*.json")) if ARCHIVE_DIR.exists() else []:
        m=re.search(r"(\d{4}-\d{2}-\d{2})",fp.name)
        if not m or not cutoff<=m.group(1)<=end:continue
        try:arr=json.loads(fp.read_text(encoding="utf-8"))
        except Exception:continue
        for a in arr:
            if key_of(a) not in seen:seen.add(key_of(a)); merged.append(a)
    return merged
def run_fetch(start=None,end=None,full_rescan=False):
    start,end=resolve_window(start,end,full_rescan); print("数据源: 东方财富公告接口"); print(f"拉取日期范围: {start}~{end}",flush=True)
    st=load_state(); old_seen=set(tuple(x) for x in st.get("seen",[])) if not full_rescan else set(); raw=fetch_all_days(start,end); normalized=[]; raw_seen=set()
    for row in raw:
        a=normalize(row); k=key_of(a)
        if a["code"] and a["title"] and k not in raw_seen:raw_seen.add(k); normalized.append(a)
    filtered=[]
    for a in normalized:
        cats=classify(a["title"])
        if cats and not is_noise(a):filtered.append({**a,"cats":cats})
    if old_seen:filtered=[a for a in filtered if key_of(a) not in old_seen]
    Path(str(OUT)+"_all.json").write_text(json.dumps(normalized,ensure_ascii=False,indent=2),encoding="utf-8"); Path(str(OUT)+"_filtered.json").write_text(json.dumps(filtered,ensure_ascii=False,indent=2),encoding="utf-8")
    archive_current(filtered,end); st=load_state(); st["seen"]=[list(key_of(a)) for a in build_universe(90,end)]; save_state(st); print(f"总计拉取 {len(normalized)} 条，重点新增 {len(filtered)} 条",flush=True); return filtered,start,end
def main():
    start=sys.argv[1] if len(sys.argv)>=3 and not sys.argv[1].startswith("--") else None; end=sys.argv[2] if len(sys.argv)>=3 and not sys.argv[2].startswith("--") else None; run_fetch(start,end,"--full" in sys.argv)
if __name__=="__main__":main()
