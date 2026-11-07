"""过滤引擎:活跃度过滤、查重由调度器完成;此处做规则+行业偏好备注判定。

行业偏好设计参考实战实例:筛选条件(extras)可配置
industries_avoid(避开行业关键词数组)、industries_prefer(优先行业关键词数组)。

判定只写备注,不拦截入库;唯一会拒绝入库的是活跃度窗口。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .activity import within_active_window
from .rules import rule_engine


@dataclass
class FilterResult:
    """对单个 JobDraft 的综合判定结果。"""

    dropped: bool = False          # 不入库(活跃度窗口外)
    drop_reason: str = ""
    reasons: list[str] = field(default_factory=list)   # 不符合原因
    pluses: list[str] = field(default_factory=list)    # 契合点(写入备注前缀)

    @property
    def remark(self) -> str:
        parts = []
        if self.pluses:
            parts.append("契合: " + ";".join(self.pluses))
        if self.reasons:
            parts.append("不符: " + ";".join(self.reasons))
        return " | ".join(parts)


def _industry_hit(industry: str, keywords: list[str]) -> list[str]:
    """行业名与关键词匹配。

    子串匹配有语义陷阱:"电商"不是"电子商务"的连续子串("子"字隔开)。
    因此关键词按字匹配、允许中间间隔最多 2 个字:
    "电商" → 电.{0,2}商,可命中"电子商务"。
    """
    if not industry or not keywords:
        return []
    hits = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        if kw.lower() in industry.lower() or re.search(
            ".{0,2}".join(re.escape(c) for c in kw), industry, re.IGNORECASE
        ):
            if kw not in hits:
                hits.append(kw)
    return hits


def evaluate_job(draft, extras: dict[str, Any] | None = None) -> FilterResult:
    """入库前综合评估:活跃度 → 行业偏好 → 关键词规则。"""
    extras = extras or {}
    result = FilterResult()

    # 1) 活跃度窗口(唯一会拒绝入库的判定)
    ok, why = within_active_window(draft.hr_active_at, draft.published_at)
    if not ok:
        result.dropped = True
        result.drop_reason = why
        return result

    industry = getattr(draft, "industry", "") or ""
    # 2) 行业偏好(硬信号):避开优先于优先
    avoid = [k for k in (extras.get("industries_avoid") or []) if k]
    prefer = [k for k in (extras.get("industries_prefer") or []) if k]
    hit_avoid = _industry_hit(industry, avoid)
    if industry and hit_avoid:
        result.reasons.append(f"行业不符: {industry}(需避开「{'、'.join(hit_avoid)}」)")
    elif industry and prefer:
        hit_prefer = _industry_hit(industry, prefer)
        if hit_prefer:
            result.pluses.append(f"行业契合: {industry}")
        else:
            result.reasons.append(f"行业非优先列表: {industry}")

    # 3) 关键词规则(总是执行)
    result.reasons.extend(rule_engine.match(draft))
    # 契合/不符标记(fit)由外部评估写入数据库,本引擎不产出
    return result


__all__ = ["FilterResult", "evaluate_job"]