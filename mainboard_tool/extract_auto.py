"""自动抽取重点公告 PDF 原文。

数据源已切换到东方财富。优先使用公告记录中的 pdf_url，
并以 art_code 构造 PDF 地址；不再依赖巨潮 static.cninfo.com.cn。
"""
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pypdf import PdfReader

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
from path_util import data_root

BASE = data_root()
PDF_DIR = BASE / "announce_pdf"
TXT_DIR = BASE / "announce_txt"
FILTERED = BASE / "cninfo_announce_filtered.json"

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/notices/",
    "Accept": "application/pdf,application/octet-stream,text/html,*/*",
}
PDF_BASE = "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
MAX_DOWNLOAD_RETRIES = 4


def load():
    if not FILTERED.exists():
        return []
    fl = json.loads(FILTERED.read_text(encoding="utf-8"))
    seen, out = set(), []
    for x in fl:
        k = (x.get("code"), x.get("title"), x.get("time"))
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def select(items):
    by_cat = defaultdict(list)
    for x in items:
        for c in x.get("cats") or []:
            if c in PRIORITY:
                by_cat[c].append(x)
    picks = []
    for c, lim in PRIORITY.items():
        picks.extend(sorted(by_cat.get(c, []), key=lambda y: y.get("time", ""), reverse=True)[:lim])
    seen, result = set(), []
    for x in picks:
        code = x.get("code")
        if code and code not in seen:
            seen.add(code)
            result.append(x)
    return result


def _safe_name(name):
    return re.sub(r"[\\/:*?\"<>|]", "", str(name or "")).strip()[:80] or "公告"


def _article_id(x):
    art = str(x.get("art_code") or x.get("_art_code") or "").strip()
    if art:
        return art
    url = str(x.get("pdf_url") or x.get("url") or "").strip()
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def _pdf_url(x):
    url = str(x.get("pdf_url") or "").strip()
    if url:
        return url
    art = str(x.get("art_code") or x.get("_art_code") or "").strip()
    return PDF_BASE.format(art_code=art) if art else ""


def _download(url, path):
    last_err = None
    for attempt in range(MAX_DOWNLOAD_RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                ctype = (r.headers.get("Content-Type") or "").lower()
            if not data.startswith(b"%PDF"):
                raise ValueError(f"返回内容不是 PDF (Content-Type={ctype}, size={len(data)})")
            path.write_bytes(data)
            return len(data)
        except Exception as e:
            last_err = e
            if attempt + 1 < MAX_DOWNLOAD_RETRIES:
                time.sleep(min(8, 1.0 * (2 ** attempt)))
    raise RuntimeError(f"PDF下载失败: {last_err}") from last_err


def _extract_text(pdf_path, txt_path):
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = re.sub(r"\s+", " ", "\n".join(parts)).strip()
    if not text:
        raise ValueError("PDF没有可提取文本")
    txt_path.write_text(text, encoding="utf-8")
    return len(text), len(reader.pages)


def process_one(x):
    code = str(x.get("code") or "").strip()
    name = _safe_name(x.get("name"))
    if not code:
        return "[SKIP] 无股票代码"
    url = _pdf_url(x)
    if not url:
        return f"[FAIL] {code} {name}: 东方财富公告没有 art_code/pdf_url"
    aid = _article_id(x)
    pdf_path = PDF_DIR / f"{code}_{aid}.pdf"
    txt_path = TXT_DIR / f"{code}_{aid}.txt"
    try:
        if txt_path.exists() and txt_path.stat().st_size > 200:
            return f"[CACHE] {code} {name} ({aid})"
        if not pdf_path.exists() or pdf_path.stat().st_size < 100:
            size = _download(url, pdf_path)
            print(f"[PDF] {code} {name}: downloaded {size} bytes", flush=True)
        chars, pages = _extract_text(pdf_path, txt_path)
        return f"[OK] {code} {name} ({aid}) pages={pages} text={chars}"
    except Exception as e:
        for p in (pdf_path, txt_path):
            try:
                if p.exists() and p.stat().st_size < 200:
                    p.unlink()
            except Exception:
                pass
        return f"[FAIL] {code} {name} ({aid}): {e}"


def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    items = load()
    if not items:
        print("[EXTRACT] 没有可处理的重点公告")
        return
    picks = select(items)
    print(f"[EXTRACT] 自动选定 {len(picks)} 条重点公告下载东方财富 PDF")
    ok = cache = fail = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(process_one, x) for x in picks]
        for f in as_completed(futures):
            msg = f.result()
            print(msg, flush=True)
            if msg.startswith("[OK]"): ok += 1
            elif msg.startswith("[CACHE]"): cache += 1
            elif msg.startswith("[FAIL]"): fail += 1
    print(f"[EXTRACT] 完成：成功 {ok}，缓存 {cache}，失败 {fail}；文本目录 {TXT_DIR}", flush=True)


if __name__ == "__main__":
    main()
