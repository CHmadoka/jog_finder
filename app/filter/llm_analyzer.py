"""LLM 自动备注分析:OpenAI 兼容 API,输出结构化判定。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..config import config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是资深招聘信息分析助手。根据求职者设定的目标岗位档位,判断职位是否匹配,"
    "并指出不匹配的具体原因(如:岗位要求不符、加班文化、压力大、频繁出差、驻场外派、"
    "薪资结构不合理、业务方向不符等)。只输出 JSON。"
)

_USER_TMPL = (
    "目标档位:{gear}\n档位词条:{keywords}\n"
    "需避开的行业/关键词:{avoid}\n优先行业/关键词:{prefer}\n\n"
    "职位标题:{title}\n公司:{company}\n行业:{industry}\n薪资:{salary}\n"
    "经验:{experience}\t学历:{education}\n\n职位描述:\n{description}\n\n"
    '请输出 JSON:{{"fit": true/false, "score": 0到1的小数, "reasons": ["不匹配原因(中文,简洁)", ...]}}\n'
    "若匹配则 reasons 为空数组;若不匹配,reasons 每条一个具体原因"
    "(如:行业不符、岗位要求不符、加班文化、压力大、频繁出差、驻场外派、薪资结构不合理等)。"
)


class LlmAnalyzer:
    """惰性初始化的 LLM 客户端;.env 未配置则禁用。"""

    def __init__(self) -> None:
        self._client: Any = None
        self.enabled = bool(config.LLM_API_KEY and config.LLM_API_BASE)

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=config.LLM_API_KEY,
                base_url=config.LLM_API_BASE or None,
            )
        return self._client

    async def analyze(self, draft, gear_name: str, keywords: list[str],
                      avoid: list[str] | None = None, prefer: list[str] | None = None) -> dict:
        client = self._ensure_client()
        description = (draft.description or "")[:3000]
        user = _USER_TMPL.format(
            gear=gear_name or "(未指定)",
            keywords="、".join(keywords) or "(无)",
            avoid="、".join(avoid or []) or "(无)",
            prefer="、".join(prefer or []) or "(无)",
            title=draft.title,
            company=draft.company,
            industry=getattr(draft, "industry", "") or "(未标注)",
            salary=draft.salary or "(未标注)",
            experience=draft.experience or "(未标注)",
            education=draft.education or "(未标注)",
            description=description or "(无描述)",
        )
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=500,
        )
        content = resp.choices[0].message.content or ""
        return self._parse_json(content)

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从模型输出中稳健地提取 JSON。"""
        text = text.strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise ValueError(f"LLM 输出非 JSON: {text[:100]}")
        obj = json.loads(m.group(0))
        if not isinstance(obj, dict) or "fit" not in obj:
            raise ValueError("LLM 输出缺少 fit 字段")
        obj.setdefault("reasons", [])
        score = obj.get("score")
        if isinstance(score, (int, float)):
            obj["score"] = max(0.0, min(1.0, float(score)))
        else:
            obj["score"] = None
        return obj


llm_analyzer = LlmAnalyzer()