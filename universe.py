"""归档公告的统一合并入口。

`gen_dashboard` / `rule_summarize` / `eastmoney_fetch` 原先各自实现了一份
「扫描 cninfo_announce_archive/filtered_*.json，按日期窗口去重合并」的逻辑，
三份在细节上并不一致（是否并入当期 filtered、缺目录/坏 JSON 的处理、
窗口边界、是否吞掉日期解析异常），同一份归档在不同环节可能算出不同结果。

这里收敛为唯一实现，三个调用方全部委托过来。本模块只依赖标准库，
以便被 scripts/ 与 mainboard_tool/ 下的脚本共同导入。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 「名称:」前缀识别。只在 prefixes 与股票简称一致时才剥离 —— 标题里形如
# 「H股公告：…」「关于…的公告：…」的前缀并不是股票名，误剥会破坏标题语义。
_NAME_PREFIX = re.compile(r"^([^:：\s]{2,12})[:：]")

# 全/半角标点归一：同一公告在归档与库里可能用不同标点（「H股公告：」vs「H股公告:」），
# 不归一就会被当成两条并排显示。
_PUNCT_MAP = str.maketrans({"：": ":", "，": ",", "（": "(", "）": ")", "、": ","})


def _strip_name_prefix(title: str, name: str) -> str:
    """仅当「名称:」前缀与股票简称一致（互为子串）时剥离，否则原样返回。"""
    t = str(title or "").strip()
    m = _NAME_PREFIX.match(t)
    if not m:
        return t
    prefix, nm = m.group(1), str(name or "").strip()
    if nm and (prefix == nm or prefix in nm or nm in prefix):
        return t[m.end():].strip()
    return t


def display_title(title, name="") -> str:
    """展示用标题：剥掉与股票简称一致的「名称:」前缀，保留原有标点。"""
    return _strip_name_prefix(title, name)


def norm_title(title, name="") -> str:
    """跨源去重键用的标题归一化（不可用于展示）。

    库内标题由 VPS 流水线写入，形如「华昌化工:关于筹划…」；归档里的同一条既没有
    前缀，标点也可能是全角。直接比对会把同一条公告算成两条，因此这里：
      1. 剥离与股票简称一致的「名称:」前缀
      2. 去掉空白、统一全/半角标点
    """
    t = _strip_name_prefix(title, name)
    return re.sub(r"\s+", "", t).translate(_PUNCT_MAP)



def key_of(row: dict) -> tuple:
    """公告去重键，与 eastmoney_fetch.key_of 保持一致。"""
    if not isinstance(row, dict):
        return (None, None, None)
    return (row.get("code"), row.get("title"), row.get("time"))


def cutoff_date(days: int, end: str) -> str:
    """窗口起始日（含）。end 非法时退化为「不设下限」。"""
    try:
        return (
            dt.date.fromisoformat(end) - dt.timedelta(days=max(0, int(days)) - 1)
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return "2000-01-01"


def iter_archive_files(archive_dir, days: int, end: str):
    """按文件名里的日期筛出窗口内的归档文件，返回 [(date, Path)]，日期升序。"""
    out = []
    root = Path(archive_dir) if archive_dir else None
    if not root or not root.exists():
        return out
    cutoff = cutoff_date(days, end)
    for fp in sorted(root.glob("filtered_*.json")):
        m = _DATE_RE.search(fp.name)
        if not m:
            continue
        day = m.group(1)
        if day < cutoff or day > end:
            continue
        out.append((day, fp))
    return out


def _load_list(fp: Path):
    """读取一个 JSON 数组文件；损坏或非数组时返回空列表（不抛错）。"""
    try:
        arr = json.loads(Path(fp).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return arr if isinstance(arr, list) else []


def merge_archive(archive_dir, days: int = 90, end_date=None, extra_files=()):
    """合并窗口内的归档公告，可选再并入额外文件（例如当期 filtered）。

    参数
    ----
    archive_dir : 存放 ``filtered_YYYY-MM-DD.json`` 的目录
    days        : 回溯窗口天数（含 end_date 当天）
    end_date    : 窗口结束日 ``YYYY-MM-DD``，默认取本地今天
    extra_files : 额外并入的 JSON 文件，按给定顺序追加在归档之后

    坏 JSON / 缺目录一律跳过而不抛错：归档是历史产物，个别文件损坏
    不应该让整条流水线失败。
    """
    end = end_date or dt.date.today().strftime("%Y-%m-%d")
    merged, seen = [], set()

    sources = [fp for _day, fp in iter_archive_files(archive_dir, days, end)]
    sources.extend(Path(fp) for fp in (extra_files or ()))

    for fp in sources:
        for a in _load_list(fp):
            if not isinstance(a, dict):
                continue
            k = key_of(a)
            if k in seen:
                continue
            seen.add(k)
            merged.append(a)
    return merged
