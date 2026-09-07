"""P1-2 调度与并发策略测试：批次并发不得污染全局并发。"""
from core.scheduler import batch_active_count, batch_limit, can_start


def mk(status="queued", batch_id="", batch_concurrency=None, tid=""):
    return {"id": tid, "status": status, "batch_id": batch_id,
            "batch_concurrency": batch_concurrency}


class TestBatchLimit:
    def test_limit_read_from_batch(self):
        tasks = [mk(batch_id="b1", batch_concurrency=2)]
        assert batch_limit(tasks, "b1") == 2

    def test_no_batch_no_limit(self):
        assert batch_limit([mk()], "") is None
        assert batch_limit([mk(batch_id="b1")], "b1") is None

    def test_invalid_limit_ignored(self):
        assert batch_limit([mk(batch_id="b1", batch_concurrency="abc")], "b1") is None

    def test_limit_floor_is_one(self):
        assert batch_limit([mk(batch_id="b1", batch_concurrency=0)], "b1") is None


class TestCanStart:
    def test_batch_does_not_change_global(self):
        # 核心：批次并发 5 > 全局 3，全局上限仍生效
        tasks = [mk("downloading", "b1", 5, f"t{i}") for i in range(3)]
        cand = mk("queued", "b1", 5, "t9")
        assert can_start(cand, tasks + [cand], global_max=3) is False

    def test_batch_cap_respected_below_global(self):
        # 批次并发 1，全局 3：批内已有 1 个活跃 → 同批不可再起
        tasks = [mk("downloading", "b1", 1, "t0")]
        cand = mk("queued", "b1", 1, "t1")
        assert can_start(cand, tasks + [cand], global_max=3) is False

    def test_other_batch_unaffected(self):
        # b1 批满，但 b2 的任务仍可启动（全局未满）
        tasks = [mk("downloading", "b1", 1, "t0")]
        cand = mk("queued", "b2", 2, "t1")
        assert can_start(cand, tasks + [cand], global_max=3) is True

    def test_single_task_only_global(self):
        tasks = [mk("downloading", tid=f"t{i}") for i in range(2)]
        cand = mk("queued", tid="t9")
        assert can_start(cand, tasks + [cand], global_max=3) is True
        assert can_start(cand, tasks + [mk("downloading", tid="t2"), cand],
                         global_max=3) is False

    def test_batch_active_count(self):
        tasks = [mk("downloading", "b1"), mk("downloading", "b1"),
                 mk("downloading", "b2"), mk("queued", "b1")]
        assert batch_active_count(tasks, "b1") == 2
        assert batch_active_count(tasks, "b2") == 1
        assert batch_active_count(tasks, "") == 0
