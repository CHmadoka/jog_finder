# 2026-11-07 重复岗位判定重构:UpsertResult 结构化返回(最佳实践)

## 背景

上一轮修复"重复岗位无字段变化被误判为新岗位而重复 INSERT"时用了
哨兵值(未命中返回 -1)。能用,但魔法数字语义靠注释约定,不显式、
易再犯——按最佳实践重构为结构化返回。

## 变更清单

1. **`UpsertResult(NamedTuple)`**(`app/scheduler/runner.py`):
   - `exists: bool` + `changed: int` 两个正交字段,类型自说明;
   - `_db_update_existing` 返回 `UpsertResult(exists=…, changed=…)`:
     未命中 `(False, 0)`,命中 `(True, changed)`,`changed` 可为 0
     (存在但无字段变化);
   - 调用方 `_process_one` 以 `up.exists` 判定"已存在",
     `up.changed` 仅用于进度文案(>0 显示"同步更新 N 个字段",
     =0 显示"已存在,无字段变化")。
2. **清理死代码**:删除无任何调用方的 `_db_exists_touch`
   (旧查重函数,职责已被 `_db_update_existing` 覆盖)。

## 验证

- 两轮抓取同一批岗位:第一轮新入库 2 条,第二轮全部
  "已存在,无字段变化"跳过、任务正常完成(修复前此处
  IntegrityError 中断);库中仍只有 2 条,无重复行。
- 未命中返回 `UpsertResult(exists=False, changed=0)`。
- unittest 64 项通过;服务已重启生效。
