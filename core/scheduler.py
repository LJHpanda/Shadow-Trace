"""调度与并发策略（P1-2）。

系统全局并发与批次并发分离：
- 全局上限 global_max 只由配置决定，批量下载弹窗中的并发值只作用于该批次；
- 任务创建时把批次并发写入任务的 batch_concurrency 字段（随 tasks.json 持久化，
  重启后仍生效）；
- 调度时对每个候选任务同时检查：全局活跃数 < global_max 且
  该批次活跃数 < 批次上限（无批次/无上限则只受全局约束）。
"""

ACTIVE_STATUS = "downloading"


def batch_limit(tasks, batch_id):
    """从同批次任意任务上读取批次并发上限；无则返回 None。"""
    if not batch_id:
        return None
    for t in tasks:
        if t.get("batch_id") == batch_id:
            c = t.get("batch_concurrency")
            if c:
                try:
                    return max(1, int(c))
                except (TypeError, ValueError):
                    return None
    return None


def batch_active_count(tasks, batch_id):
    if not batch_id:
        return 0
    return sum(1 for t in tasks
               if t.get("batch_id") == batch_id and t.get("status") == ACTIVE_STATUS)


def can_start(task, tasks, global_max):
    """判断 task 现在能否启动：全局与批次双重约束。"""
    active = sum(1 for t in tasks if t.get("status") == ACTIVE_STATUS)
    if active >= global_max:
        return False
    bid = task.get("batch_id") or ""
    limit = batch_limit(tasks, bid)
    if limit is not None and batch_active_count(tasks, bid) >= limit:
        return False
    return True
