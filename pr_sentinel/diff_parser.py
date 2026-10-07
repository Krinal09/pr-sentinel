"""Parse GitHub unified-diff patches.

GitHub only accepts inline review comments on lines that appear in the diff,
so we need to know exactly which new-file line numbers are commentable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ParsedPatch:
    path: str
    commentable_lines: set[int] = field(default_factory=set)  # new-file lines (RIGHT side)
    added_lines: dict[int, str] = field(default_factory=dict)
    annotated: str = ""  # patch with new-file line numbers, given to the LLM


def parse_patch(path: str, patch: str) -> ParsedPatch:
    result = ParsedPatch(path=path)
    out: list[str] = []
    new_line = 0
    in_hunk = False

    for raw in (patch or "").splitlines():
        match = HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(3))
            in_hunk = True
            out.append(raw)
            continue
        if not in_hunk or raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            result.commentable_lines.add(new_line)
            result.added_lines[new_line] = raw[1:]
            out.append(f"{new_line:>5} + {raw[1:]}")
            new_line += 1
        elif raw.startswith("-"):
            out.append(f"{'':>5} - {raw[1:]}")
        else:
            text = raw[1:] if raw.startswith(" ") else raw
            result.commentable_lines.add(new_line)
            out.append(f"{new_line:>5}   {text}")
            new_line += 1

    result.annotated = "\n".join(out)
    return result


def nearest_commentable(line: int, commentable: set[int], max_distance: int = 3) -> int | None:
    """LLMs are sometimes off by a line or two; snap to the closest valid line."""
    if line in commentable:
        return line
    best = None
    for candidate in commentable:
        dist = abs(candidate - line)
        if dist <= max_distance and (best is None or dist < abs(best - line)):
            best = candidate
    return best
