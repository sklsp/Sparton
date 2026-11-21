"""Unit tests for the path-safety helpers in app.core.security.paths.

Every browser-supplied filename and every user-built path goes through these
two functions before it touches disk; the upload, safetensors and JSON helpers
in the same module are tested here as well. A regression here is not cosmetic:
it is either a file written outside its designated directory, or an upload name
that becomes a Windows device name. The tests pin the promises in the
docstrings of sanitize_filename and safe_join with literal values.
"""

from __future__ import annotations

import json

import pytest

from app.core.security.paths import (
    UnsafePathError,
    is_safetensors,
    read_json,
    safe_join,
    sanitize_filename,
    validate_image_upload,
    write_json_atomic,
)

#: Five megabytes, the limit used by the upload size tests.
MAX_BYTES = 5 * 1024 * 1024


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
            (root / "link.txt").symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"this OS refuses to create symlinks here: {exc}")
        with pytest.raises(UnsafePathError):
            safe_join(root, "link.txt")

    def test_an_empty_component_raises(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(UnsafePathError):
            safe_join(root, "")


class TestValidateImageUpload:
    @pytest.mark.parametrize("extension", [".png", ".jpg", ".jpeg"])
    def test_each_allowed_extension_is_accepted(self, extension):
        assert validate_image_upload(f"photo{extension}", 1024, MAX_BYTES) == f"photo{extension}"

    @pytest.mark.parametrize(
        ("filename", "message"),
        [
            ("photo.gif", "Unsupported image type '.gif'. Allowed: .jpeg, .jpg, .png"),
            # No extension at all: the message shows the name, not an empty string.
            ("README", "Unsupported image type 'README'. Allowed: .jpeg, .jpg, .png"),
        ],
    )
    def test_a_refused_extension_raises_with_the_allowed_list(self, filename, message):
        with pytest.raises(ValueError) as excinfo:
            validate_image_upload(filename, 1024, MAX_BYTES)
        assert str(excinfo.value) == message

    def test_an_empty_file_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            validate_image_upload("photo.png", 0, MAX_BYTES)
        assert str(excinfo.value) == "Uploaded file is empty"

    def test_a_size_exactly_at_the_limit_is_accepted(self):
        assert validate_image_upload("photo.png", MAX_BYTES, MAX_BYTES) == "photo.png"

    def test_a_size_above_the_limit_raises_with_both_numbers(self):
        with pytest.raises(ValueError) as excinfo:
            validate_image_upload("photo.png", 6 * 1024 * 1024, MAX_BYTES)
        assert str(excinfo.value) == "File is 6.0 MB; the limit is 5 MB"


class TestIsSafetensors:
    @staticmethod
    def _header(payload: bytes) -> bytes:
        # The format is an 8-byte little-endian length, then that many JSON bytes.
        return len(payload).to_bytes(8, "little") + payload

    def test_a_real_minimal_header_is_recognised(self, tmp_path):
        path = tmp_path / "model.safetensors"
        path.write_bytes(self._header(b'{"__metadata__": {"format": "pt"}}'))
        assert is_safetensors(path) is True

    def test_a_truncated_file_is_not_safetensors(self, tmp_path):
        # Fewer than 8 bytes: not even a length field.
        path = tmp_path / "model.safetensors"
        path.write_bytes(b"\x02\x00")
        assert is_safetensors(path) is False

    @pytest.mark.parametrize(
        "data",
        [
            # Declared header length of zero fails the 0 < length check.
            (0).to_bytes(8, "little"),
            # Declared length larger than the whole file.
            (100).to_bytes(8, "little") + b"{}",
            # Declared length above the sane ceiling even though the file is tiny.
            (100_000_001).to_bytes(8, "little") + b"{}",
        ],
    )
    def test_a_wrong_length_is_not_safetensors(self, tmp_path, data):
        path = tmp_path / "model.safetensors"
        path.write_bytes(data)
        assert is_safetensors(path) is False

    def test_json_that_is_not_an_object_is_not_safetensors(self, tmp_path):
        path = tmp_path / "model.safetensors"
        path.write_bytes(self._header(b"[1]"))
        assert is_safetensors(path) is False

    def test_a_missing_file_is_not_safetensors(self, tmp_path):
        assert is_safetensors(tmp_path / "nope.safetensors") is False


class TestWriteJsonAtomic:
    def test_it_writes_the_payload(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    def test_a_second_write_overwrites_the_first(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, {"a": 1})
        write_json_atomic(target, {"b": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"b": 2}

    def test_no_temp_file_is_left_behind(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, {"a": 1})
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_missing_parent_folders_are_created(self, tmp_path):
        target = tmp_path / "a" / "b" / "state.json"
        write_json_atomic(target, {"ok": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

    def test_a_crash_during_the_write_leaves_no_temp_file(self, tmp_path):
        class Boom:
            def __str__(self):
                raise RuntimeError("cannot serialize")

        target = tmp_path / "state.json"
        with pytest.raises(RuntimeError, match="cannot serialize"):
            write_json_atomic(target, {"x": Boom()})
        assert list(tmp_path.iterdir()) == []


class TestReadJson:
    def test_a_missing_file_gives_the_default(self, tmp_path):
        assert read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}

    def test_broken_json_gives_the_default(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        assert read_json(path, {"d": 1}) == {"d": 1}

    def test_valid_json_is_returned_as_parsed(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"a": [1, 2]}', encoding="utf-8")
        assert read_json(path) == {"a": [1, 2]}
