"""自动抽取重点公告PDF原文（无token版）
不依赖手写KEY列表：根据 cninfo_announce_filtered.json 的类别标签，
自动挑选各优先类别下的重点公司，下载PDF并提取文本到 announce_txt/。
已存在的文本文件直接复用，不重复下载。
"""
import json, re, time, urllib.request
import sys
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypdf import PdfReader

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root, resource_root

BASE = data_root()
PDF_DIR = BASE / "announce_pdf"
TXT_DIR = BASE / "announce_txt"
FILTERED = BASE / "cninfo_announce_filtered.json"

# 优先抽取类别及每类上限（控制在合理耗时内）
PRIORITY = {
    "并购重组": 15,
    "出售/转让": 12,
    "人事变动": 10,
    "质押/解押": 12,
    "立案/处罚": 12,
    "退市风险": 8,
    "破产重整": 8,
    "重大诉讼": 12,
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}


def load():
    fl = json.loads(FILTERED.read_text(encoding="utf-8"))
    seen, out = set(), []
    for x in fl:
        k = (x.get("code"), x.get("title"))
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def select(items):
    by_cat = defaultdict(list)
    for x in items:
        for c in (x.get("cats") or []):
            if c in PRIORITY:
                by_cat[c].append(x)
    picks = []
    for c, lim in PRIORITY.items():
        lst = sorted(by_cat.get(c, []), key=lambda y: y.get("time", ""), reverse=True)[:lim]
        picks.extend(lst)
    seen, res = set(), []
    for x in picks:
        if x["code"] in seen:
            continue
        seen.add(x["code"])
        res.append(x)
    return res


def find_txt(code):
    cands = [p for p in TXT_DIR.glob(f"{code}_*.txt")]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_size)


def process_one(x):
    """下载单只 PDF 并抽取文本，返回状态字符串。"""
    code = x["code"]
    name = x.get("name", "").replace("*", "").replace("/", "").replace("\\", "").replace(":", "").strip()
    url = x.get("url", "")
    if not url:
        return f"[SKIP] {code} {name}: 无URL"
    pdf_path = PDF_DIR / f"{code}_{name}.PDF"
    txt_path = TXT_DIR / f"{code}_{name}.txt"
    existing = find_txt(code)
    if txt_path.exists() or (existing and existing.stat().st_size > 200):
        return f"[CACHE] {code} {name}"
    try:
        if not pdf_path.exists():
            u = "http://static.cninfo.com.cn/" + url
            req = urllib.request.Request(u, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                pdf_path.write_bytes(r.read())
        if not txt_path.exists():
            reader = PdfReader(str(pdf_path))
            t = ""
            for pg in reader.pages[:6]:
                t += (pg.extract_text() or "") + "\n"
            t = re.sub(r"\s+", " ", t).strip()
            txt_path.write_text(t, encoding="utf-8")
        return f"[OK] {code} {name}"
    except Exception as e:
        return f"[FAIL] {code} {name}: {e}"


def main():
    PDF_DIR.mkdir(exist_ok=True)
    TXT_DIR.mkdir(exist_ok=True)
    items = load()
    picks = select(items)
    print(f"自动选定 {len(picks)} 只重点公司以下载PDF原文")
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(process_one, x) for x in picks]
        for f in as_completed(futs):
            msg = f.result()
            if msg.startswith("[SKIP]") or msg.startswith("[FAIL]"):
                done += 0
            else:
                done += 1
            print(msg, flush=True)
    print(f"完成：本次处理 {done} 条，文本存于 announce_txt/")


if __name__ == "__main__":
    main()
