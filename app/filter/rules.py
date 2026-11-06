"""关键词规则引擎:自动发现岗位描述中的"不符合"信号。

规则库参考 2026-11 Boss 直聘实战筛选(boss-search 实例)验证过的信号词。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..platforms.base import JobDraft


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    label: str
    category: str


def _r(pattern: str, label: str, category: str) -> Rule:
    return Rule(re.compile(pattern), label, category)


# 规则库:pattern → 备注话术(可按需扩充)
_RULES: list[Rule] = [
    _r(r"996|007|大小周|单休|义务加班|自愿加班", "存在加班文化信号", "加班"),
    _r(r"高强度|压力大|抗压能力强|快节奏|狼性|内卷|末位淘汰|绩效淘汰", "工作强度/压力大", "压力"),
    _r(r"长期出差|经常出差|全国出差|出差频率高", "长期出差", "出差"),
    _r(r"驻场|驻厂|外派|派驻|异地办公|工作地点服从分配", "驻场/外派/异地", "地点"),
    _r(r"提成|业绩指标|业绩考核|销售任务|销售指标|开单|底薪[+加]提|无底薪|零底薪", "含销售/业绩考核信号", "销售"),
    _r(r"培训.{0,6}(费|押金|保证金)|(费|押金|保证金).{0,6}培训", "涉及培训收费/押金,谨慎", "风险"),
    _r(r"需要倒班|夜班|两班倒|三班倒|倒班|轮班|值班", "需要倒班/值班", "作息"),
    _r(r"30岁以上|35岁以上|不超过\s*35|年龄不超过", "年龄限制信号", "风险"),
    _r(r"五险一金.{0,10}(面议|转正后)", "社保公积金转正后才缴纳", "福利"),
    _r(r"拉新|地推|电销|陌拜", "含销售/地推成分", "方向不符"),
    _r(r"外包|劳务派遣|人力外包", "外包/派遣性质(节奏取决于甲方)", "性质"),
]

# 仅作用于标题的规则(兼职/实习等)
_TITLE_RULES: list[Rule] = [
    _r(r"兼职|实习|小时工|劳务", "兼职/实习岗(非全职)", "性质"),
    _r(r"资深|高级|专家|架构|首席|总监", "资深/管理级岗位", "级别"),
]


class RuleEngine:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = rules or _RULES
        self.title_rules = _TITLE_RULES

    def match(self, draft: JobDraft) -> list[str]:
        """扫描标题+描述,返回去重后的原因列表。"""
        text = f"{draft.title}\n{draft.description}"
        found: list[str] = []
        for rule in self.rules:
            if rule.pattern.search(text):
                msg = f"[{rule.category}] {rule.label}"
                if msg not in found:
                    found.append(msg)
        for rule in self.title_rules:
            if rule.pattern.search(draft.title or ""):
                msg = f"[{rule.category}] {rule.label}"
                if msg not in found:
                    found.append(msg)
        return found


rule_engine = RuleEngine()