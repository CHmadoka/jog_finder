"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

PLATFORMS = ("boss", "job51", "liepin")


class GearIn(BaseModel):
    name: str = Field(default="", max_length=100)   # 缺省时自动命名 新岗位设置_n
    keywords: list[str] = Field(default_factory=list)


class GearOut(GearIn):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SearchFilterIn(BaseModel):
    platform: str
    name: str = "默认条件"
    city: str = ""
    gear_id: Optional[int] = None
    extras: dict[str, Any] = Field(default_factory=dict)


class SearchFilterOut(SearchFilterIn):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TaskOut(BaseModel):
    id: int
    platform: str
    status: str
    message: str
    found: int
    saved: int
    skipped: int
    job_count: int = 0           # 关联岗位数(crawl_task_id 指向本任务;删除确认/免确认依据)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    filter_snapshot: dict[str, Any] = Field(default_factory=dict)
    keywords: str = ""            # 全部岗位匹配词条(以|分隔)
    city: str = ""
    salary_range: str = ""        # 薪资范围原文(如 10-20 / 10, / ,20)
    skip_stats: dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True


class JobOut(BaseModel):
    id: int
    platform: str
    platform_job_id: str
    title: str
    company: str
    salary: str
    salary_months: Optional[int] = None
    city: str
    district: str
    experience: str
    education: str
    description: str
    company_intro: str
    hr_name: str
    hr_title: str
    hr_active_at: Optional[str] = None       # 估算字符串,后缀携带方向:"…后/…前/…活跃/无"
    hr_active_text: str = ""                 # 平台原始活跃文案
    hr_active_label: str = ""                # 由估算规则生成的状态标签(接口层填充)
    published_at: Optional[datetime] = None
    url: str
    gear_id: Optional[int] = None
    keyword: str
    source_keyword: str = ""                 # 搜索来源词条(由哪个词条的搜索结果中抓出)
    industry: str = ""
    company_stage: str = ""
    company_scale: str = ""
    welfare: str = ""
    hr_online: Optional[bool] = None
    remark: str
    fit: Optional[bool] = None
    fit_score: Optional[float] = None
    first_seen_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    crawl_task_id: Optional[int] = None      # 产生/最后更新本岗位的抓取任务
    table_id: Optional[int] = None           # 所属数据表(空=默认表)

    class Config:
        from_attributes = True


class JobPage(BaseModel):
    total: int
    items: list[JobOut]


class TableIn(BaseModel):
    name: str = Field(default="", max_length=100)   # 缺省时自动命名 数据表_{id}
    slug: str = Field(default="", max_length=100)


class TableOut(BaseModel):
    id: int
    name: str
    slug: str
    created_at: Optional[datetime] = None
    job_count: int = 0

    class Config:
        from_attributes = True


class StartTaskIn(BaseModel):
    platform: str
    filter_id: Optional[int] = None
    max_pages: Optional[int] = None
    city: Optional[str] = None