"""
Unit tests for file editor, code runner, and terminal tools.
"""

from __future__ import annotations

import tempfile

from thwip.tools import ToolManager


def test_file_editor_operations():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ToolManager(project_path=tmpdir)

        # 1. Write file
        res = manager.file_editor.write_file("hello.txt", "line1\nline2\nline3")
        assert "Successfully wrote" in res

        # 2. Read file
        content = manager.file_editor.read_file("hello.txt")
        assert "line1\nline2\nline3" in content

        # 3. Edit file
        edit_res = manager.file_editor.edit_file("hello.txt", "line2", "line2_updated")
        assert "Successfully updated" in edit_res

        updated_content = manager.file_editor.read_file("hello.txt")
        assert "line2_updated" in updated_content

        # 4. List files
        list_res = manager.file_editor.list_files()
        assert "hello.txt" in list_res


def test_code_runner_python():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ToolManager(project_path=tmpdir)
        res = manager.code_runner.run_python("print('Hello from Thwip')")
        assert "Hello from Thwip" in res


def test_file_editor_blocks_paths_outside_workspace(tmp_path):
    manager = ToolManager(project_path=tmp_path / "project")
    manager.file_editor.project_path.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")

    assert "outside the project workspace" in manager.file_editor.read_file(str(outside))
    assert "outside the project workspace" in manager.file_editor.write_file("../outside.txt", "changed")
    assert outside.read_text() == "private"
