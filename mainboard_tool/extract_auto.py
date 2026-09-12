"""自动抽取重点公告 PDF/正文原文。

优先下载东方财富 PDF；PDF 不可用时，使用东方财富公告正文接口作为兜底。
这样公告详情页或 PDF CDN 临时异常时，报告仍可生成。
"""
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
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
ARCHIVE_DIR = BASE / "cninfo_announce_archive"

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
    "Accept": "application/json,application/pdf,application/octet-stream,text/html,*/*",
}
PDF_BASE = "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
CONTENT_API = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
MAX_RETRIES = 4

# 正文最长保留字符数：公告正文里偶尔会内嵌大段脚本/样式/JSON，
# 即使正则清洗过，也再兜一层上限，避免下游 extract_numbers 在超大文本上跑正则。
MAX_TEXT_CHARS = 200_000


def _safe_name(name):
    return re.sub(r"[\\/:*?\"<>|]", "", str(name or "")).strip()[:80] or "公告"


def _load_json_file(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load():
    """当前筛选不存在时，从最新归档恢复，避免临时网络失败导致本地整条链路断掉。"""
    source = FILTERED
    if not source.exists() or source.stat().st_size == 0:
        archives = sorted(ARCHIVE_DIR.glob("filtered_*.json"), reverse=True) if ARCHIVE_DIR.exists() else []
        if archives:
            source = archives[0]
            print(f"[EXTRACT] 当前筛选缓存不存在，使用最新归档：{source.name}")
    if not source.exists():
        return []
    fl = _load_json_file(source) or []
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


def _article_id(x):
    art = str(x.get("art_code") or x.get("_art_code") or "").strip()
    if art:
        return art
    url = str(x.get("pdf_url") or x.get("url") or "").strip()
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] if url else hashlib.sha1(
        f"{x.get('code','')}|{x.get('title','')}|{x.get('time','')}".encode("utf-8")
    ).hexdigest()[:20]


def _pdf_url(x):
    url = str(x.get("pdf_url") or "").strip()
    if url:
        return url
    art = str(x.get("art_code") or x.get("_art_code") or "").strip()
    return PDF_BASE.format(art_code=art) if art else ""


def _download(url, path):
    last_err = None
    for attempt in range(MAX_RETRIES):
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
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(8, 2 ** attempt))
    raise RuntimeError(f"PDF下载失败: {last_err}") from last_err


def _content_text(x):
    art = str(x.get("art_code") or x.get("_art_code") or "").strip()
    if not art:
        return ""
    params = urllib.parse.urlencode({"art_code": art, "client_source": "web", "page_index": "1"})
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(CONTENT_API + "?" + params, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                obj = json.loads(r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace"))
            data = obj.get("data") or {}
            content = str(data.get("notice_content") or "")
            title = str(data.get("notice_title") or x.get("title") or "")
            if not content:
                raise ValueError("东方财富正文接口没有 notice_content")
            # 正文通常为 HTML；转成纯文本供现有规则总结器使用。
            # 注意：这些是 raw string，反斜杠必须只写一个。写成 r"<script[\\s\\S]*?</script>"
            # 时正则实际匹配的是 "\"、"s"、"S" 三个字符，script/style 块永远删不掉，
            # 会把整段脚本漏进正文并显著放大 txt 体积。
            text = re.sub(r"<script[\s\S]*?</script>", " ", content, flags=re.I)
            text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
            text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
            text = re.sub(r"</(?:p|div|tr|li|h[1-6])>", "\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
            return (title + "\n" + text).strip()
        except Exception as e:
            last_err = e
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(8, 2 ** attempt))
    raise RuntimeError(f"东方财富正文接口失败: {last_err}") from last_err


def _extract_text(pdf_path, txt_path):
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = re.sub(r"\s+", " ", "\n".join(parts)).strip()
    if not text:
        raise ValueError("PDF没有可提取文本")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    txt_path.write_text(text, encoding="utf-8")
    return len(text), len(reader.pages)


def process_one(x):
    code = str(x.get("code") or "").strip()
    name = _safe_name(x.get("name"))
    if not code:
        return "[SKIP] 无股票代码"
    aid = _article_id(x)
    pdf_path = PDF_DIR / f"{code}_{aid}.pdf"
    txt_path = TXT_DIR / f"{code}_{aid}.txt"
    try:
        if txt_path.exists() and txt_path.stat().st_size > 200:
            return f"[CACHE] {code} {name} ({aid})"
        pdf_url = _pdf_url(x)
        if pdf_url:
            try:
                if not pdf_path.exists() or pdf_path.stat().st_size < 100:
                    size = _download(pdf_url, pdf_path)
                    print(f"[PDF] {code} {name}: downloaded {size} bytes", flush=True)
                chars, pages = _extract_text(pdf_path, txt_path)
                return f"[OK-PDF] {code} {name} ({aid}) pages={pages} text={chars}"
            except Exception as pdf_err:
                print(f"[PDF-FALLBACK] {code} {name}: {pdf_err}", flush=True)
        text = _content_text(x)
        if text:
            txt_path.write_text(text, encoding="utf-8")
            return f"[OK-TEXT] {code} {name} ({aid}) text={len(text)}"
        raise RuntimeError("PDF和正文接口均未返回有效内容")
    except Exception as e:
        return f"[FAIL] {code} {name} ({aid}): {e}"


def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    items = load()
    if not items:
        print("[EXTRACT] 没有可处理的重点公告")
        return
    picks = select(items)
    print(f"[EXTRACT] 自动选定 {len(picks)} 条重点公告")
    ok = cache = fail = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(process_one, x) for x in picks]
        for f in as_completed(futures):
            msg = f.result()
            print(msg, flush=True)
            if msg.startswith("[OK-"): ok += 1
            elif msg.startswith("[CACHE]"): cache += 1
            elif msg.startswith("[FAIL]"): fail += 1
    print(f"[EXTRACT] 完成：有效 {ok}，缓存 {cache}，失败 {fail}；文本目录 {TXT_DIR}", flush=True)


if __name__ == "__main__":
    main()
