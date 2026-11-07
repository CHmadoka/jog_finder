"""SQLAlchemy 实体定义。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def now_local() -> datetime:
    """本地时间(单机工具统一使用本地时区,与活跃估算/页面解析的 now 一致)。"""
    return datetime.now()


class JobTable(Base):
    """数据表(岗位分组容器):id + 表名 + 标识符。id=1 为默认表,不可删除。"""

    __tablename__ = "job_table"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, comment="表名")
    slug = Column(String(100), nullable=False, unique=True, comment="标识符")
    created_at = Column(DateTime, default=now_local)


class Gear(Base):
    """岗位档位:一个标签名 + 多个具体岗位词条。"""

    __tablename__ = "gear"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, comment="档位标签名")
    keywords = Column(JSON, nullable=False, default=list, comment="岗位词条列表")
    created_at = Column(DateTime, default=now_local)


class SearchFilter(Base):
    """某平台的搜索条件(城市 + 档位 + 附加参数)。"""

    __tablename__ = "search_filter"
    __table_args__ = (UniqueConstraint("platform", "name", name="uq_filter_platform_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False, comment="boss/job51/liepin")
    name = Column(String(100), nullable=False, default="默认条件")
    city = Column(String(50), default="", comment="城市")
    gear_id = Column(Integer, nullable=True, comment="关联档位")
    keyword = Column(String(200), default="", comment="直接搜索词(优先于档位词条)")
    extras = Column(JSON, default=dict, comment="平台附加参数(经验/学历/薪资范围等)")
    created_at = Column(DateTime, default=now_local)


class Job(Base):
    """岗位信息。查重键:(platform, platform_job_id)。"""

    __tablename__ = "job"
    __table_args__ = (UniqueConstraint("platform", "platform_job_id", name="uq_job_platform_jid"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False, index=True)
    platform_job_id = Column(String(100), nullable=False, index=True)
    title = Column(String(200), default="")
    company = Column(String(200), default="")
    salary = Column(String(50), default="")
    salary_months = Column(Integer, default=12, comment="年薪月数,如 16 薪")
    city = Column(String(50), default="")
    district = Column(String(50), default="")
    experience = Column(String(50), default="")
    education = Column(String(50), default="")
    description = Column(Text, default="", comment="职位描述全文")
    company_intro = Column(Text, default="")
    hr_name = Column(String(100), default="")
    hr_title = Column(String(100), default="")
    # HR 活跃:平台不给精确时间,存估算字符串(后缀携带方向),精确到分:
    #   "YYYY-MM-DD HH:MM后"=真实活跃不早于该时刻;"…前"=不晚于该时刻;
    #   "YYYY-MM-DD 活跃"=今日/昨日(仅日期);无后缀=约精确时刻
    hr_active_at = Column(String(32), default="", comment="估算的HR活跃时刻(含方向后缀)")
    hr_active_text = Column(String(50), default="", comment="平台原始活跃文案,如: 本周活跃/3日内活跃")
    extra = Column(JSON, default=dict, comment="打开原始页所需参数(如 Boss securityId/lid,防重定向)")
    published_at = Column(DateTime, nullable=True, comment="岗位发布时间")
    url = Column(String(500), default="")
    gear_id = Column(Integer, nullable=True, comment="命中的档位")
    keyword = Column(String(200), default="", comment="抓取时使用的搜索词")
    source_keyword = Column(
        String(100), default="", comment="搜索来源词条(该岗位由哪个词条的搜索结果中抓出)",
    )
    crawl_task_id = Column(
        Integer, ForeignKey("crawl_task.id"), nullable=True,
        comment="产生/最后更新本岗位的抓取任务(外键)",
    )
    table_id = Column(
        Integer, ForeignKey("job_table.id"), nullable=True,
        comment="所属数据表(空=默认表)",
    )
    industry = Column(String(100), default="", comment="公司行业")
    company_stage = Column(String(50), default="", comment="融资阶段")
    company_scale = Column(String(50), default="", comment="公司规模")
    welfare = Column(String(300), default="", comment="福利标签")
    hr_online = Column(Boolean, default=False, comment="HR当前在线")
    remark = Column(Text, default="", comment="不符合原因备注(自动生成)")
    fit = Column(Boolean, nullable=True, comment="LLM/规则判定是否匹配,None=未分析")
    fit_score = Column(Float, nullable=True, comment="匹配度 0-1")
    first_seen_at = Column(DateTime, default=now_local, index=True)
    updated_at = Column(DateTime, default=now_local, onupdate=now_local)


class CrawlTask(Base):
    """抓取任务记录。"""

    __tablename__ = "crawl_task"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False, index=True)
    filter_snapshot = Column(JSON, default=dict, comment="任务使用的条件快照")
    # 条件专用列(便于按抓取记录查询;快照保留完整明细)
    keywords = Column(Text, default="", comment="全部岗位匹配词条(以|分隔)")
    city = Column(String(50), default="", comment="抓取城市")
    salary_range = Column(String(50), default="", comment="薪资范围(存用户输入原文,如 10-20 / 10, / ,20;单位K/月,单边可缺省)")
    status = Column(String(20), default="running", comment="running/done/stopped/error")
    message = Column(Text, default="", comment="结束原因或错误信息")
    found = Column(Integer, default=0, comment="发现岗位数")
    saved = Column(Integer, default=0, comment="新入库数")
    skipped = Column(Integer, default=0, comment="因活跃度/查重跳过数")
    skip_stats = Column(JSON, default=dict, comment="跳过原因统计 {原因: 数量}")
    started_at = Column(DateTime, default=now_local)
    finished_at = Column(DateTime, nullable=True, comment="完成时间(为空=意外中断或中止,仅 done 时写入)")


class AppEvent(Base):
    """运行期异常/警告事件(左下角异常面板;手动清除前持久存在)。"""

    __tablename__ = "app_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), default="", index=True, comment="boss/job51/liepin/空=全局")
    level = Column(String(10), default="warn", comment="warn/error")
    message = Column(Text, default="", comment="异常描述")
    url = Column(String(500), default="", comment="相关岗位/页面链接(点击跳转)")
    created_at = Column(DateTime, default=now_local, index=True)