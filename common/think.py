"""Reasoning/answer splitting for harness-facing output.

Reasoning models (nemotron, qwen3, r1-style) emit chain-of-thought
terminated by ``</think>``. Chat templates usually consume the opening
``<think>`` tag, so raw output looks like ``reasoning</think>answer``
with no opening marker. OSS harnesses (jcode, opencode, Cline) render
the DeepSeek-style ``reasoning_content`` delta field as collapsible
thinking; leaking raw think text into ``content`` pollutes transcripts.

This module is the single normalization point:

- ``split_think(text)``: exact split of a *complete* output.
- ``StreamSplitter``: incremental splitter for SSE. Fed the full
  partial text each poll (monotonically growing), returns the
  (reasoning_delta, content_delta) pair safe to emit now. Handles the
  missing-opening-tag case via a family hint, withholds partial-tag
  boundaries, and falls back gracefully when the hint is wrong.

Server-side only; nodes keep streaming raw text (the dash and the
code harness want the unmodified transcript).
"""

from __future__ import annotations

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"
_HOLD = len(CLOSE_TAG) - 1  # max chars a partial tag can occupy at the tail

# Model families whose chat template consumes <think> and which emit
# reasoning-first output. Cheap substring match on ModelSpec.family.
REASONING_FAMILY_MARKERS = (
    "nemotron", "qwen3", "r1", "gpt-oss", "reason", "think", "o1", "o3",
)

# Sniff window: without a tag or family hint, commit to content mode
# after this many chars (keeps first-token latency bounded).
SNIFF_CHARS = 256


def is_reasoning_family(family: str | None) -> bool:
    if not family:
        return False
    name = family.lower()
    return any(m in name for m in REASONING_FAMILY_MARKERS)


def split_think(text: str) -> tuple[str, str]:
    """Split a complete output into (reasoning, content).

    Rules: first ``</think>`` terminates reasoning (opening tag
    optional). No closing tag: everything is content, unless the text
    opens with ``<think>`` (truncated reasoning, content empty).
    """
    idx = text.find(CLOSE_TAG)
    if idx == -1:
        stripped = text.lstrip()
        if stripped.startswith(OPEN_TAG):
            return stripped[len(OPEN_TAG):].strip("\n"), ""
        return "", text
    head, tail = text[:idx], text[idx + len(CLOSE_TAG):]
    head_stripped = head.lstrip()
    if head_stripped.startswith(OPEN_TAG):
        head_stripped = head_stripped[len(OPEN_TAG):]
    return head_stripped.strip("\n"), tail.lstrip("\n")


class StreamSplitter:
    """Incremental reasoning/content splitter for a growing partial.

    Usage per SSE poll::

        splitter.set_family(node_family)      # no-op once mode locked
        r_delta, c_delta = splitter.feed(partial, done=is_terminal)

    Deltas are monotonic append-only slices; emit ``r_delta`` as a
    ``reasoning_content`` delta and ``c_delta`` as ``content``.
    """

    def __init__(self, assume_reasoning: bool | None = None) -> None:
        self._assume = assume_reasoning
        self._mode: str | None = None  # None (sniffing) | "reasoning" | "content"
        self._r_sent = 0
        self._c_sent = 0

    @property
    def locked(self) -> bool:
        return self._mode is not None

    def set_family(self, family: str | None) -> None:
        """Provide the assigned node's model family before mode lock."""
        if self._mode is None and self._assume is None and family:
            self._assume = is_reasoning_family(family)

    def feed(self, partial: str, done: bool = False) -> tuple[str, str]:
        text = partial
        if self._mode is None:
            stripped = text.lstrip()
            # A short head that is still a prefix of "<think>" is ambiguous.
            if (not done and stripped and len(stripped) < len(OPEN_TAG)
                    and OPEN_TAG.startswith(stripped)):
                return "", ""
            if stripped.startswith(OPEN_TAG) or CLOSE_TAG in text:
                self._mode = "reasoning"
            elif self._assume:
                self._mode = "reasoning"
            elif done or self._assume is False or len(text) >= SNIFF_CHARS:
                self._mode = "content"
            else:
                return "", ""  # keep sniffing

        if self._mode == "content":
            return "", self._emit_content(self._strip_tags(text, done))

        # reasoning mode
        idx = text.find(CLOSE_TAG)
        if idx != -1:
            reasoning = self._strip_open(text[:idx])
            content = text[idx + len(CLOSE_TAG):].lstrip("\n")
            if not done:
                content = content[:max(0, len(content) - _HOLD)]
            return self._emit_reasoning(reasoning), self._emit_content(content)
        if not done:
            visible = self._strip_open(text[:max(0, len(text) - _HOLD)])
            return self._emit_reasoning(visible), ""
        # done, no closing tag ever arrived
        if text.lstrip().startswith(OPEN_TAG):
            # genuinely truncated reasoning: keep content empty
            return self._emit_reasoning(self._strip_open(text)), ""
        if self._r_sent == 0:
            # nothing emitted yet: the reasoning assumption was wrong,
            # reclassify wholesale as content
            self._mode = "content"
            return "", self._emit_content(text)
        # Already streamed as reasoning and the close tag never came:
        # the generation was cut mid-reasoning (max_tokens). Keep
        # content empty; duplicating CoT into content would reintroduce
        # exactly the leak this module exists to prevent.
        return self._emit_reasoning(self._strip_open(text)), ""

    # ------------------------------------------------------------- internals

    @staticmethod
    def _strip_open(head: str) -> str:
        stripped = head.lstrip()
        if stripped.startswith(OPEN_TAG):
            return stripped[len(OPEN_TAG):].lstrip("\n")
        return head

    @staticmethod
    def _strip_tags(text: str, done: bool) -> str:
        if not done:
            text = text[:max(0, len(text) - _HOLD)]
        return text.replace(OPEN_TAG, "").replace(CLOSE_TAG, "")

    def _emit_reasoning(self, total: str) -> str:
        delta = total[self._r_sent:]
        self._r_sent = len(total)
        return delta

    def _emit_content(self, total: str) -> str:
        delta = total[self._c_sent:]
        self._c_sent = len(total)
        return delta
