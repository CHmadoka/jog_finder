"""平台适配器共用解析工具。"""
from __future__ import annotations

import re


def strip_html(html: str) -> str:
    """HTML → 纯文本(保留换行)。"""
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    # 压缩连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def deep_find_list(node, keys: list[str], depth: int = 0):
    """在嵌套 JSON(SSR 数据)中查找第一个命中候选键且元素为岗位 dict 的数组。"""
    if depth > 9 or node is None:
        return None
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            if any(k in node[0] for k in keys) or "jobName" in node[0] or "jobTitle" in node[0]:
                return node
            for item in node:
                found = deep_find_list(item, keys, depth + 1)
                if found:
                    return found
        return None
    if isinstance(node, dict):
        for k in keys:
            v = node.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in node.values():
            found = deep_find_list(v, keys, depth + 1)
            if found:
                return found
    return None


def parse_salary_months(salary: str) -> int | None:
    """从"1.5-2.5万·13薪"/"15-25K·16薪"提取月数。"""
    m = re.search(r"[·x×X×](\d{1,2})薪", salary or "")
    return int(m.group(1)) if m else None


def parse_salary_k(salary: str) -> tuple[float, float] | None:
    """把薪资文本解析为 (最低K, 最高K)/月;无法解析返回 None。

    支持:"15-25K·16薪" / "1.5-2.5万" / "20K" / "2万-3万" / "9千-1.5万"(两侧
    单位可不同,分别换算)/ "30-60万/年"(折算到月)。
    日薪("元/天")与"面议"等无法换算为月薪的文本返回 None。
    """
    s = (salary or "").strip().replace(",", "")
    if not s or "面议" in s:
        return None
    # 年薪折算到月(取近似值,仅用于范围过滤)
    m = re.search(r"([\d.]+)\s*[-~至]\s*([\d.]+)\s*万\s*[/每]\s*年", s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo * 10 / 12, hi * 10 / 12)
    # 区间:两侧单位可不同("9千-1.5万" = 9K~15K);缺省单位按高值侧
    m = re.search(
        r"([\d.]+)\s*(万|千|[kK])?\s*[-~至]\s*([\d.]+)\s*(万|千|[kK])", s
    )
    if m:
        def unit_mul(u: str | None, default: str) -> float:
            u = u or default
            return 10 if u == "万" else 1
        lo, hi = float(m.group(1)), float(m.group(3))
        lo, hi = lo * unit_mul(m.group(2), m.group(4)), hi * unit_mul(m.group(4), m.group(4))
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)
    m = re.search(r"([\d.]+)\s*(万|千|[kK])", s)
    if m:
        v = float(m.group(1)) * (10 if m.group(2) == "万" else 1)
        return (v, v)
    return None


def parse_salary_range(text: str) -> tuple[float | None, float | None] | None:
    """解析配置的薪资范围字符串,单边可缺省。

    分隔符宽容:支持 "," "-" "~" "～" "，"(存储保留用户输入原文,仅比较时解析)。
    返回 (min, max),单位 K/月;无法解析返回 None。
    """
    s = (text or "").strip()
    if not s:
        return None
    parts = re.split(r"[,，\-~～]", s)
    try:
        if len(parts) == 1 and parts[0].strip():   # 单个数字 = 仅有下限
            return (float(parts[0]), None)
        if len(parts) != 2:
            return None
        lo = float(parts[0]) if parts[0].strip() else None
        hi = float(parts[1]) if parts[1].strip() else None
    except ValueError:
        return None
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def salary_in_range(salary: str, rng: str) -> bool | None:
    """判断岗位薪资与配置范围是否有交集。None=任一侧无法解析(不过滤)。

    有交集(部分重叠也算)返回 True,完全在范围外返回 False。
    """
    cfg = parse_salary_range(rng)
    if cfg is None:
        return None
    job = parse_salary_k(salary)
    if job is None:
        return None
    lo, hi = cfg
    if lo is not None and job[1] < lo:
        return False
    if hi is not None and job[0] > hi:
        return False
    return True


async def read_window_global(page, names: list[str]):
    """依次尝试读取 window 上的 SSR 全局变量,返回第一个非空的。"""
    for name in names:
        var = name.split(".", 1)[1] if "." in name else name
        try:
            v = await page.evaluate(
                f"() => {{ try {{ return window['{var}'] ?? null }} catch(e) {{ return null }} }}"
            )
            if v:
                return v
        except Exception:
            continue
    return None