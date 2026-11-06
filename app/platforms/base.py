"""平台适配器抽象基类与数据容器。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Awaitable, Callable

from playwright.async_api import Page

ProgressCallback = Callable[[str], None]
SkipCheck = Callable[[str], Awaitable[bool]]


@dataclass
class JobDraft:
    """适配器产出的岗位原始数据。"""

    platform_job_id: str
    title: str = ""
    url: str = ""
    company: str = ""
    salary: str = ""
    salary_months: int | None = None
    city: str = ""
    district: str = ""
    experience: str = ""
    education: str = ""
    description: str = ""
    company_intro: str = ""
    hr_name: str = ""
    hr_title: str = ""
    hr_active_at: str = ""                    # 估算字符串(含"前/后/活跃"后缀,见 utils.active_estimate_from_text)
    hr_active_text: str = ""                  # 平台原始活跃文案,如"本周活跃"
    open_params: dict = field(default_factory=dict)  # 打开原始页所需参数(如 Boss securityId/lid,防重定向)
    published_at: datetime | None = None
    industry: str = ""           # 公司行业(Boss 接口 brandIndustry 等)
    company_stage: str = ""      # 融资阶段
    company_scale: str = ""      # 公司规模
    welfare: str = ""            # 福利标签(顿号连接)
    hr_online: bool = False      # HR 当前在线
    raw: dict = field(default_factory=dict)


class LoginRequired(Exception):
    """平台登录态失效。"""


class AntiBot(Exception):
    """触发风控/验证,需要人工介入。"""


class BasePlatform(ABC):
    """各招聘平台适配器接口。

    约定:适配器只负责"驱动浏览器并解析出 JobDraft",
    查重、活跃度过滤、备注生成由调度器与过滤引擎负责。
    """

    id: str = ""
    name: str = ""
    home_url: str = ""
    home_prefix: str = ""

    @abstractmethod
    async def is_logged_in(self, page: Page) -> bool:
        """探测当前浏览器中该平台是否已登录。"""

    @abstractmethod
    async def iter_jobs(
        self,
        page: Page,
        *,
        city: str,
        keyword: str,
        extras: dict,
        max_pages: int,
        detail_page: Page | None = None,
        progress: ProgressCallback | None = None,
        skip_check: SkipCheck | None = None,
    ) -> AsyncIterator[JobDraft]:
        """执行搜索并逐条产出岗位(自动翻页,最多 max_pages 页)。

        skip_check: 可选的异步回调(job_id -> bool)。返回 True 表示岗位已存在,
        适配器将跳过详情页抓取(避免对已入库岗位重复打开页面,降低风控压力)。
        """

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "home_url": self.home_url}