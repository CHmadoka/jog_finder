"""平台适配器包。"""
from __future__ import annotations

from .base import BasePlatform, JobDraft

_REGISTRY: dict[str, BasePlatform] = {}


def register(p: BasePlatform) -> BasePlatform:
    _REGISTRY[p.id] = p
    return p


def get_platform(pid: str) -> BasePlatform:
    if pid not in _REGISTRY:
        raise KeyError(f"未知平台: {pid},可用: {list(_REGISTRY)}")
    return _REGISTRY[pid]


def all_platforms() -> list[BasePlatform]:
    return list(_REGISTRY.values())


__all__ = ["BasePlatform", "JobDraft", "get_platform", "all_platforms", "register"]


def _lazy_register() -> None:
    # 延迟导入避免循环依赖;各平台模块自行调用 register()
    from . import boss as _boss  # noqa: F401
    from . import job51 as _job51  # noqa: F401
    from . import liepin as _liepin  # noqa: F401


_lazy_register()