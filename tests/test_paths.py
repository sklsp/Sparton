"""Unit tests for the path-safety helpers in app.core.security.paths.

Every browser-supplied filename and every user-built path goes through these
two functions before it touches disk. A regression here is not cosmetic: it is
either a file written outside its designated directory, or an upload name that
becomes a Windows device name. The tests pin the promises in the docstrings of
sanitize_filename and safe_join with literal values.
"""

from __future__ import annotations

import os

import pytest

from app.core.security.paths import UnsafePathError, safe_join, sanitize_filename


class TestSanitizeFilename:
    def test_a_plain_name_passes_through_unchanged(self):
        assert sanitize_filename("report.png") == "report.png"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # POSIX traversal collapses to the last component.
            ("../../etc/passwd", "passwd"),
            # Windows separators get the same treatment.
            (r"..\\..\\windows\\system32\\x", "x"),
            ("a/b/c.txt", "c.txt"),
        ],
    )
    def test_directory_portions_are_stripped_under_both_separator_conventions(self, raw, expected):
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Control characters are outside the safe set.
            ("bad\nname.txt", "bad_name.txt"),
            ("a\x00b.png", "a_b.png"),
            # Spaces and non-ASCII letters are replaced, not dropped.
            ("sp ace.jpg", "sp_ace.jpg"),
            ("café.png", "caf_.png"),
        ],
    )
    def test_characters_outside_the_safe_set_are_replaced_with_underscores(self, raw, expected):
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Windows reserves these device names regardless of extension.
            ("CON.txt", "_CON.txt"),
            ("con", "_con"),
            ("aux.log", "_aux.log"),
            ("COM1", "_COM1"),
            ("LPT9.csv", "_LPT9.csv"),
            ("nul.png", "_nul.png"),
        ],
    )
    def test_reserved_windows_device_names_get_an_underscore_prefix(self, raw, expected):
        assert sanitize_filename(raw) == expected

    def test_a_name_that_only_resembles_a_device_is_left_alone(self):
        # Only the bare device stems are reserved; "console" is an ordinary word.
        assert sanitize_filename("console.png") == "console.png"

    @pytest.mark.parametrize("raw", ["", "...", "///", r"..\\.."])
    def test_input_that_sanitizes_to_nothing_falls_back_to_the_default(self, raw):
        assert sanitize_filename(raw) == "file"

    def test_a_custom_default_is_used_for_empty_input(self):
        assert sanitize_filename("", default="fallback") == "fallback"

    def test_very_long_names_are_cut_to_120_characters(self):
        # Callers may append suffixes afterwards, so the cut leaves room for them.
        result = sanitize_filename("a" * 200 + ".png")
        assert len(result) == 120
        assert result == "a" * 120


class TestSafeJoin:
    def test_a_normal_join_stays_under_the_root(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert safe_join(root, "a", "b.txt") == (root / "a" / "b.txt").resolve()

    def test_a_single_component_is_allowed(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert safe_join(root, "file.png") == (root / "file.png").resolve()

    @pytest.mark.parametrize(
        "parts",
        [
            ("..",),
            # The docstring names this shape explicitly: a/../../b.
            ("a", "..", ".."),
            ("a/../../b",),
        ],
    )
    def test_dotdot_traversal_that_escapes_the_root_raises(self, tmp_path, parts):
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(UnsafePathError):
            safe_join(root, *parts)

    @pytest.mark.parametrize(
        "part",
        [
            "/etc/passwd",
            # Drive letter with a path.
            "C:/Windows/system32",
            # A drive letter without a slash is still absolute on Windows.
            "c:somefile",
        ],
    )
    def test_absolute_parts_and_drive_letters_raise(self, tmp_path, part):
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(UnsafePathError):
            safe_join(root, part)

    def test_a_unc_prefix_raises(self, tmp_path):
        # The docstring promises UNC prefixes are rejected too.
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(UnsafePathError):
            safe_join(root, "\\\\server\\share")

    def test_a_symlink_pointing_outside_the_root_raises(self, tmp_path):
        # The check runs after resolution, so a link inside the root that points
        # outside it must be caught even though every part looks relative.
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        root = tmp_path / "root"
        root.mkdir()
        try:
            os.symlink(outside, root / "link.txt")
        except OSError as exc:
            pytest.skip(f"this OS refuses to create symlinks here: {exc}")
        with pytest.raises(UnsafePathError):
            safe_join(root, "link.txt")
