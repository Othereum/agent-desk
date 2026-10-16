"""Frontmatter of state.md and knowledge entries."""

from __future__ import annotations

import re

from .core import DeskError


# Restricted YAML subset: `key: value` and flow lists `[a, "b, c"]`.

def _parse_list(raw: str) -> list[str]:
    items, buf, quoted, i = [], "", False, 0
    body = raw.strip()[1:-1]
    while i < len(body):
        c = body[i]
        if c == '"':
            quoted = not quoted
        elif c == "," and not quoted:
            items.append(buf.strip())
            buf = ""
        else:
            buf += c
        i += 1
    if buf.strip():
        items.append(buf.strip())
    return [x for x in items if x]


def _format_value(value) -> str:
    if isinstance(value, list):
        def q(s: str) -> str:
            return f'"{s}"' if any(ch in s for ch in ',[]"') else s
        return "[" + ", ".join(q(str(v).replace('"', "'")) for v in value) + "]"
    return str(value)


def split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise DeskError("frontmatter가 없습니다")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return lines[1:end], "\n".join(lines[end + 1:])
    raise DeskError("frontmatter가 닫히지 않았습니다")


def parse_frontmatter(text: str) -> dict:
    fm_lines, _ = split_frontmatter(text)
    data = {}
    for line in fm_lines:
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition(":")
        # an inline comment needs two spaces before `#`, so a title like "PR #12" survives
        raw = re.split(r"\s{2,}#", raw, maxsplit=1)[0].strip()
        data[key.strip()] = _parse_list(raw) if raw.startswith("[") and raw.endswith("]") else raw
    return data


def update_frontmatter(text: str, updates: dict) -> str:
    fm_lines, body = split_frontmatter(text)
    pending = dict(updates)
    out = []
    for line in fm_lines:
        key = line.partition(":")[0].strip()
        if key in pending:
            out.append(f"{key}: {_format_value(pending.pop(key))}".rstrip())
        else:
            out.append(line)
    out.extend(f"{k}: {_format_value(v)}".rstrip() for k, v in pending.items())
    return "---\n" + "\n".join(out) + "\n---\n" + body
