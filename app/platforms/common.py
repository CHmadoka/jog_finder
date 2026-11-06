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