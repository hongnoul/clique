"""P0 tests: harness fence extraction, guards, prompt packing, workspaces."""

from __future__ import annotations

import pytest

from common.errors import ProtocolError
from common.types import CodeTaskSpec
from scheduler import harness
from scheduler.workspaces import WorkspaceError, WorkspaceManager

GOOD = """here is the fix
```diff
--- a/foo.py
+++ b/foo.py
@@ -1 +1 @@
-old
+new
```
done"""


def test_extract_ok():
    assert harness.extract_diff(GOOD).startswith("--- a/foo.py")


def test_extract_missing():
    with pytest.raises(ProtocolError, match="diff_missing"):
        harness.extract_diff("no fence here")


def test_extract_multi():
    with pytest.raises(ProtocolError, match="multiple"):
        harness.extract_diff(GOOD + "\n```diff\n--- a/x\n```")


def test_validate_path_forbidden():
    spec = CodeTaskSpec(files={"foo.py": "x"})
    bad = "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@\n-x\n+y\n"
    with pytest.raises(ProtocolError, match="path_forbidden"):
        harness.validate_diff(bad, spec)


def test_validate_allowed_paths():
    spec = CodeTaskSpec(files={"a.py": "x"}, allowed_paths=["src/"])
    bad = "--- a/a.py\n+++ b/a.py\n@@\n-x\n+y\n"
    with pytest.raises(ProtocolError, match="not in allowed_paths"):
        harness.validate_diff(bad, spec)


def test_validate_budget():
    spec = CodeTaskSpec(files={"a.py": "x"}, patch_budget_kb=1)
    big = "--- a/a\n+++ b/a\n@@\n" + "+x\n" * 2000
    with pytest.raises(ProtocolError, match="patch_too_large"):
        harness.validate_diff(big, spec)


def test_validate_test_cmd():
    spec = CodeTaskSpec(files={"a.py": "x"}, test_cmd=["rm", "-rf"])
    with pytest.raises(ProtocolError, match="test_cmd_forbidden"):
        harness.validate_diff("--- a/a.py\n+++ b/a.py\n@@\n-x\n+y\n", spec)


def test_prompt_truncates(tmp_path=None):
    big = "y" * 100_000
    spec = CodeTaskSpec(files={"big.py": big, "small.py": "x"})
    prompt = harness.build_code_prompt("fix it", spec, context_budget_chars=1000)
    assert len(prompt) <= 1400
    assert "small.py" in prompt or "[truncated]" in prompt


def test_prompt_tools_flag():
    off = harness.build_code_prompt(
        "fix", CodeTaskSpec(files={"a.py": "x"}))
    on = harness.build_code_prompt(
        "fix", CodeTaskSpec(files={"a.py": "x"}, use_tools=True))
    assert "TOOLS:" not in off
    assert "TOOLS:" in on


def test_workspace_create_apply(tmp_path):
    mgr = WorkspaceManager(tmp_path / "ws")
    spec = CodeTaskSpec(files={"foo.py": "old\n"},
                        test_cmd=["pytest", "-q"])
    mgr.create("t-1", spec)
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    mgr.apply_patch("t-1", diff)
    assert (mgr.path_for("t-1") / "foo.py").read_text() == "new\n"
    mgr.cleanup("t-1")
    assert not mgr.path_for("t-1").exists()


def test_workspace_rejects_binary(tmp_path):
    mgr = WorkspaceManager(tmp_path / "ws")
    with pytest.raises(WorkspaceError, match="binary_rejected"):
        mgr.create("t-2", CodeTaskSpec(files={"b.bin": "a\x00b"}))
