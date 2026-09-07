"""P0-2 / P0-3 任务文件清单与回收站测试。

全部使用 tmp_path 假文件 + 注入 trash_fn，不触碰真实系统回收站。
"""
from core import trash as trashmod
from core.trash import (
    RESULT_MISSING, RESULT_RETAINED, RESULT_TRASHED,
    is_temp_file, permanent_delete_temp, summarize,
    task_file_manifest, trash_files,
)


def _fake_trash_ok(path):
    import os
    os.remove(path)
    return RESULT_TRASHED


def _fake_trash_fail(path):
    return RESULT_RETAINED


class TestTrashFiles:
    def test_trash_failure_keeps_file(self, tmp_path):
        # P0-3 核心：回收站失败 → retained，文件仍在磁盘
        f = tmp_path / "video.mp4"
        f.write_bytes(b"data")
        results = trash_files([f], within_dir=tmp_path, trash_fn=_fake_trash_fail)
        assert results[0]["result"] == RESULT_RETAINED
        assert f.exists()  # 绝不永久删除
        s = summarize(results)
        assert s["retained"] == 1 and not s["all_ok"]

    def test_partial_success_reported_per_file(self, tmp_path):
        ok_f = tmp_path / "a.mp4"; ok_f.write_bytes(b"1")
        bad_f = tmp_path / "b.mp4"; bad_f.write_bytes(b"2")
        miss_f = tmp_path / "c.mp4"  # 不存在

        def mixed(path):
            p = str(path)
            if p.endswith("a.mp4"):
                return _fake_trash_ok(path)
            if p.endswith("b.mp4"):
                return RESULT_RETAINED
            return RESULT_MISSING

        results = trash_files([ok_f, bad_f, miss_f], within_dir=tmp_path, trash_fn=mixed)
        by = {r["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: r["result"] for r in results}
        assert by["a.mp4"] == RESULT_TRASHED
        assert by["b.mp4"] == RESULT_RETAINED
        assert by["c.mp4"] == RESULT_MISSING
        assert bad_f.exists()

    def test_outside_dir_rejected(self, tmp_path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside") / "x.mp4"
        outside.write_bytes(b"x")
        results = trash_files([outside], within_dir=tmp_path, trash_fn=_fake_trash_ok)
        assert results[0]["result"] == "failed"
        assert results[0]["reason"] == "outside_output_dir"
        assert outside.exists()

    def test_send_to_trash_missing(self, tmp_path):
        assert trashmod.send_to_trash(tmp_path / "nope.mp4") == RESULT_MISSING


class TestManifest:
    def test_same_prefix_not_matched(self, tmp_path):
        # P0-2 核心：删除 "01" 不得误伤 "01-花絮"、"012"
        target = tmp_path / "01.mp4"; target.write_bytes(b"t")
        sibling1 = tmp_path / "01-花絮.mp4"; sibling1.write_bytes(b"s")
        sibling2 = tmp_path / "012.mp4"; sibling2.write_bytes(b"s")
        task = {"output_name": "01.mp4", "output_dir": str(tmp_path)}
        files = task_file_manifest(task, str(tmp_path))
        assert str(target) in files
        assert str(sibling1) not in files
        assert str(sibling2) not in files

    def test_files_field_first_priority(self, tmp_path):
        task = {"files": [str(tmp_path / "real.mkv")],
                "file_path": str(tmp_path / "real.mkv"),
                "output_name": "other.mp4", "output_dir": str(tmp_path)}
        files = task_file_manifest(task, str(tmp_path))
        assert files == [str(tmp_path / "real.mkv")]

    def test_legacy_fallback_exact_candidates_only(self, tmp_path):
        # 旧任务无 files/file_path：仅精确候选（改扩展名），不 glob
        mkv = tmp_path / "old.mkv"; mkv.write_bytes(b"m")
        noise = tmp_path / "old_extra.mp4"; noise.write_bytes(b"n")
        task = {"output_name": "old.mp4", "output_dir": str(tmp_path)}
        files = task_file_manifest(task, str(tmp_path))
        assert str(mkv) in files
        assert str(noise) not in files

    def test_empty_task_empty_manifest(self, tmp_path):
        assert task_file_manifest({}, str(tmp_path)) == []


class TestPermanentDeleteTemp:
    def test_only_whitelisted_temp(self, tmp_path):
        mp4 = tmp_path / "final.mp4"; mp4.write_bytes(b"v")
        # 正式成品文件拒绝永久删除
        assert permanent_delete_temp(mp4, tmp_path) is False
        assert mp4.exists()

    def test_temp_inside_dir_deleted(self, tmp_path):
        part = tmp_path / "video.mp4.part"; part.write_bytes(b"p")
        assert is_temp_file(part)
        assert permanent_delete_temp(part, tmp_path) is True
        assert not part.exists()

    def test_temp_outside_dir_rejected(self, tmp_path, tmp_path_factory):
        other = tmp_path_factory.mktemp("other")
        part = other / "video.part"; part.write_bytes(b"p")
        assert permanent_delete_temp(part, tmp_path) is False
        assert part.exists()

    def test_is_temp_file_rules(self):
        assert is_temp_file("a.part")
        assert is_temp_file("a.ytdl")
        assert is_temp_file("a.tmp.mp4")
        assert is_temp_file("x-Frag0012")
        assert not is_temp_file("movie.mp4")
        assert not is_temp_file("partition.mp4")  # "part" 子串不误判
