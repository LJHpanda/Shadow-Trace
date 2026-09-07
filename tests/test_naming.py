"""P0-1 文件名与路径安全测试。"""
import pytest

from core.naming import sanitize_filename, resolve_within, is_within


class TestSanitizeFilename:
    def test_path_traversal_stripped(self):
        # 目录穿越输入不得产生任何路径成分
        out = sanitize_filename("..\\..\\evil.mp4")
        assert "\\" not in out and "/" not in out
        assert ".." not in out
        assert out.endswith(".mp4")

    def test_absolute_path_stripped(self):
        out = sanitize_filename("C:\\Windows\\System32\\cmd.mp4")
        assert ":" not in out and "\\" not in out
        assert out.endswith(".mp4")

    def test_illegal_chars_replaced(self):
        out = sanitize_filename('a<b>c:d"e/f\\g|h?i*j.mp4')
        for ch in '<>:"/\\|?*':
            assert ch not in out

    def test_control_chars_removed(self):
        out = sanitize_filename("bad\x00name\x1f\x7f.mp4")
        assert "\x00" not in out and "\x1f" not in out and "\x7f" not in out
        assert out == "badname.mp4"

    @pytest.mark.parametrize("name", ["CON", "NUL", "com1", "LPT9", "aux"])
    def test_windows_reserved_names(self, name):
        out = sanitize_filename(name + ".mp4")
        stem = out.rsplit(".", 1)[0]
        assert stem.upper() not in {"CON", "NUL", "COM1", "LPT9", "AUX"}
        assert out.endswith(".mp4")

    def test_overlong_stem_truncated(self):
        out = sanitize_filename("x" * 500 + ".mp4")
        stem = out.rsplit(".", 1)[0]
        assert len(stem) <= 120
        assert out.endswith(".mp4")

    def test_empty_input_gets_default(self):
        out = sanitize_filename("")
        assert out.endswith(".mp4")
        assert len(out) > 4

    def test_only_illegal_input_gets_default(self):
        out = sanitize_filename("???///\\\\")
        assert out.endswith(".mp4")
        stem = out.rsplit(".", 1)[0]
        assert stem  # 非空主体

    def test_trailing_dot_space_removed(self):
        out = sanitize_filename("name . .mp4")
        assert not out.rsplit(".", 1)[0].endswith((" ", "."))

    def test_unknown_ext_appends_default(self):
        out = sanitize_filename("video.exe")
        assert out.endswith(".mp4")

    def test_normal_name_preserved(self):
        assert sanitize_filename("正常视频名01.mp4") == "正常视频名01.mp4"


class TestResolveWithin:
    def test_normal_file_ok(self, tmp_path):
        p = resolve_within(tmp_path, "a.mp4")
        assert p.parent == tmp_path.resolve()

    def test_dotdot_escape_raises(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "..\\outside.mp4")

    def test_posix_dotdot_escape_raises(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "../outside.mp4")

    def test_deep_escape_raises(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "..\\..\\..\\Windows\\evil.mp4")

    def test_absolute_path_raises(self, tmp_path, tmp_path_factory):
        other = tmp_path_factory.mktemp("other")
        with pytest.raises(ValueError):
            resolve_within(tmp_path, str(other / "x.mp4"))

    def test_windows_absolute_path_raises_on_every_platform(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_within(tmp_path, "C:\\Windows\\evil.mp4")

    def test_is_within(self, tmp_path):
        inner = tmp_path / "sub" / "f.mp4"
        assert is_within(tmp_path, inner)
        assert not is_within(tmp_path, tmp_path.parent / "brother.mp4")
