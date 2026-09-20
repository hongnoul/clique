"""Unit tests for common.think (reasoning/content splitting) and
contract tests for the OpenAI-compat adapter shape.

The StreamSplitter tests replay realistic streaming patterns:
- template-consumed opening tag (reasoning first, no ``<think>``)
- explicit ``<think>...</think>`` output
- plain models (no think tags at all)
- wrong family hints and truncated reasoning
The adapter-level SSE behavior is covered in test_extended.py against
a live server; these tests pin the parsing core.
"""

from __future__ import annotations

import pytest

from common.think import (
    SNIFF_CHARS,
    StreamSplitter,
    is_reasoning_family,
    split_think,
)


def drive(splitter: StreamSplitter, text: str, family: str | None = None,
          step: int = 7) -> tuple[str, str]:
    """Feed text incrementally, return accumulated (reasoning, content)."""
    r_all, c_all = [], []
    if family is not None:
        splitter.set_family(family)
    for i in range(step, len(text) + step, step):
        r, c = splitter.feed(text[:i], done=False)
        r_all.append(r)
        c_all.append(c)
    r, c = splitter.feed(text, done=True)
    r_all.append(r)
    c_all.append(c)
    return "".join(r_all), "".join(c_all)


# ---------------------------------------------------------------- split_think

def test_split_complete_missing_open_tag():
    r, c = split_think("thinking hard</think>the answer")
    assert r == "thinking hard"
    assert c == "the answer"


def test_split_complete_with_open_tag():
    r, c = split_think("<think>\nplan\n</think>\nanswer")
    assert r == "plan"
    assert c == "answer"


def test_split_plain_output():
    r, c = split_think("just an answer")
    assert r == ""
    assert c == "just an answer"


def test_split_truncated_reasoning():
    r, c = split_think("<think>never finished")
    assert r == "never finished"
    assert c == ""


# ------------------------------------------------------------ StreamSplitter

def test_stream_missing_open_tag_with_family_hint():
    text = "We must reply ok.</think>ok"
    r, c = drive(StreamSplitter(), text, family="nemotron-3-nano")
    assert r == "We must reply ok."
    assert c == "ok"


def test_stream_explicit_tags_no_hint():
    text = "<think>plan steps</think>\nfinal answer"
    r, c = drive(StreamSplitter(), text)
    assert r == "plan steps"
    assert c == "final answer"


def test_stream_plain_model_no_hint():
    text = "hello there, a plain reply"
    r, c = drive(StreamSplitter(), text, family="qwen2.5-coder")
    assert r == ""
    assert c == text


def test_stream_plain_model_sniff_commits_after_window():
    text = "x" * (SNIFF_CHARS + 50)
    splitter = StreamSplitter()
    r, c = splitter.feed(text[:SNIFF_CHARS], done=False)
    assert r == ""
    assert len(c) > 0  # committed to content mode without waiting for done


def test_stream_wrong_reasoning_hint_falls_back_to_content():
    # Family says reasoning but the model never emits </think> and the
    # splitter emitted nothing yet: final flush reclassifies as content.
    splitter = StreamSplitter(assume_reasoning=True)
    r1, c1 = splitter.feed("ok", done=False)
    assert (r1, c1) == ("", "")  # held back (within tag-hold window)
    r2, c2 = splitter.feed("ok", done=True)
    assert r2 == ""
    assert c2 == "ok"


def test_stream_reasoning_hint_truncated_think():
    text = "<think>partial reasoning that never closes"
    r, c = drive(StreamSplitter(), text, family="nemotron")
    assert "partial reasoning" in r
    assert c == ""


def test_stream_content_never_contains_close_tag():
    text = "reasoning body</think>clean answer"
    r, c = drive(StreamSplitter(), text, family="qwen3-30b", step=3)
    assert "</think>" not in c
    assert "</think>" not in r
    assert c == "clean answer"
    assert r == "reasoning body"


def test_stream_deltas_are_monotonic_append_only():
    text = "abc def ghi</think>jkl mno"
    splitter = StreamSplitter(assume_reasoning=True)
    seen_r, seen_c = "", ""
    for i in range(1, len(text) + 1):
        r, c = splitter.feed(text[:i], done=(i == len(text)))
        seen_r += r
        seen_c += c
    assert seen_r == "abc def ghi"
    assert seen_c == "jkl mno"


def test_stream_partial_open_tag_held():
    splitter = StreamSplitter()
    assert splitter.feed("<thi", done=False) == ("", "")
    assert not splitter.locked


@pytest.mark.parametrize("family,expected", [
    ("nemotron-3-nano-fp8-30", True),
    ("qwen3-30b", True),
    ("deepseek-r1", True),
    ("qwen2.5-coder", False),
    ("echo", False),
    (None, False),
    ("", False),
])
def test_is_reasoning_family(family, expected):
    assert is_reasoning_family(family) is expected
