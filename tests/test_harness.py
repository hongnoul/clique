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


def test_extract_diff_strips_think_reasoning():
    """Reasoning models draft diffs inside <think>; only the answer
    after </think> counts (live nemotron regression)."""
    out = ("thinking about it...\n```diff\n--- a/draft.py\n+++ b/draft.py\n"
           "@@\n-x\n+y\n```\nmore thoughts\n</think>\n" + GOOD)
    assert harness.extract_diff(out).startswith("--- a/foo.py")


def test_bare_hunk_header_normalized(tmp_path):
    """Live models emit ``@@`` with no ranges; patch(1) calls that
    garbage. The workspace normalizer fills ranges from the target."""
    from common.types import CodeTaskSpec
    from scheduler.workspaces import WorkspaceManager

    wm = WorkspaceManager(tmp_path)
    spec = CodeTaskSpec(
        files={"mathutil.py": "def is_odd(n):\n    return n % 2 == 1\n"},
        test_cmd=["pytest", "-q"])
    wm.create("t-bare", spec)
    live_diff = ("--- a/mathutil.py\n+++ b/mathutil.py\n@@\n"
                 " def is_odd(n):\n     return n % 2 == 1\n"
                 "+\n+def is_even(n):\n+    return n % 2 == 0\n")
    wm.apply_patch("t-bare", live_diff)
    text = (wm.path_for("t-bare") / "mathutil.py").read_text()
    assert "def is_even" in text and "def is_odd" in text


def test_wellformed_hunk_header_untouched(tmp_path):
    """Diffs with proper ranges apply exactly as before."""
    from common.types import CodeTaskSpec
    from scheduler.workspaces import WorkspaceManager

    wm = WorkspaceManager(tmp_path)
    spec = CodeTaskSpec(files={"a.py": "x = 1\ny = 2\n"},
                        test_cmd=["pytest", "-q"])
    wm.create("t-good", spec)
    diff = ("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n"
            "-x = 1\n+x = 3\n y = 2\n")
    wm.apply_patch("t-good", diff)
    assert (wm.path_for("t-good") / "a.py").read_text() == "x = 3\ny = 2\n"
