"""Frontmatter of state.md.

A defined subset of YAML, read strictly so a file is never silently misread:
- `key: value` at column 0. Values are strings: plain, "double-quoted" (\\ \" \n \t escapes)
  or 'single-quoted' ('' escapes). An empty value is "".
- Lists: flow `[a, "b, c"]`, or block items under an empty `key:` (`  - item`).
- Comments: a line starting with `#`, or `#` after whitespace outside quotes (standard YAML).
Anything else (nested maps, block scalars `|` `>`, anchors, flow maps, duplicate keys) is
an error. Writes quote values that would be ambiguous and replace only the lines of the keys
being updated, so comments and block lists of other keys survive.
"""

from __future__ import annotations

import re

from .core import DeskError

_KEY = re.compile(r"([A-Za-z_][\w.-]*):(?=\s|$)(.*)$")
_ITEM = re.compile(r"(\s*-)(?:\s+(.*))?$")
_PLAIN_BAD_START = set("-?:,[]{}#&*!|>'\"%@`")
_UNSUPPORTED_START = set("{&*!|>%@`")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "/": "/", "0": "\0"}


def split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise DeskError("no frontmatter")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return lines[1:end], "\n".join(lines[end + 1:])
    raise DeskError("the frontmatter is not closed")


# ---------------------------------------------------------------- read

def _quoted(s: str, i: int) -> tuple[str, int]:
    """The quoted string starting at s[i]; returns (value, index after the closing quote)."""
    q, out, i = s[i], [], i + 1
    while i < len(s):
        c = s[i]
        if q == "'" and c == "'":
            if s[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        if q == '"' and c == "\\":
            esc = s[i + 1:i + 2]
            if esc not in _ESCAPES:
                raise ValueError(f"unsupported escape \\{esc}")
            out.append(_ESCAPES[esc])
            i += 2
            continue
        if q == '"' and c == '"':
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise ValueError("unclosed quote")


def _plain_end(s: str, i: int, stops: str = "") -> int:
    """End of a plain scalar starting at s[i]: a comment (`#` after whitespace) or a stop char."""
    j = i
    while j < len(s):
        if s[j] in stops or (s[j] == "#" and j > 0 and s[j - 1] in " \t"):
            break
        j += 1
    return j


def _scalar(s: str, i: int, stops: str = "") -> tuple[str, int]:
    """A scalar starting at s[i] (after leading space); returns (value, end index)."""
    if i >= len(s):
        return "", i
    c = s[i]
    if c in "\"'":
        return _quoted(s, i)
    if c in _UNSUPPORTED_START or (stops and c in "[{#"):
        raise ValueError(f"unsupported YAML syntax ({c})")
    value = s[i:_plain_end(s, i, stops)].rstrip()
    return value, i + len(value)


def _flow_list(s: str, i: int) -> tuple[list[str], int]:
    """A flow list starting at s[i] == '['; returns (items, index after ']')."""
    items, i = [], i + 1
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            raise ValueError("the list has no ]")
        if s[i] == "]":
            return items, i + 1
        value, i = _scalar(s, i, stops=",[]{}")
        while i < len(s) and s[i] in " \t":
            i += 1
        if i < len(s) and s[i] == ",":
            items.append(value)
            i += 1
        elif i < len(s) and s[i] == "]":
            items.append(value)
            return items, i + 1
        else:
            raise ValueError("list items need a comma between them")


def _value(raw: str) -> tuple[str | list[str], str]:
    """The value after `key:`; returns (value, trailing comment)."""
    i = len(raw) - len(raw.lstrip(" \t"))
    if i >= len(raw) or raw[i] == "#":
        return "", raw.rstrip()
    if raw[i] == "[":
        value, end = _flow_list(raw, i)
    elif raw[i] == "-" and raw[i + 1:i + 2] in ("", " ", "\t"):
        raise ValueError("write a list as `  - item` lines below the key, or as [a, b]")
    else:
        value, end = _scalar(raw, i)
    rest = raw[end:]
    if rest.strip():
        if not rest.lstrip().startswith("#") or rest[:1] not in (" ", "\t"):
            raise ValueError(f"unexpected text after the value: {rest.strip()}")
    return value, rest.rstrip()


def _parse(fm_lines: list[str]) -> tuple[dict, dict]:
    """Returns (data, spans). spans[key]: start/end line indexes (end exclusive, covering block
    items), kind (scalar | flow | empty | block; items may follow only empty or block) and the
    key line's trailing comment."""
    data, spans, key = {}, {}, None
    for n, line in enumerate(fm_lines):
        try:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            item = _ITEM.match(line)
            if item and key and spans[key]["kind"] in ("empty", "block"):
                sp = spans[key]
                value, comment = _value(item.group(2) or "")
                if isinstance(value, list):
                    raise ValueError("nested lists are not supported")
                if item.group(2) and item.group(2)[0] not in "\"'" and re.search(r":(\s|$)", value):
                    raise ValueError("`key: value` inside a list item (a nested structure) is not supported; quote it if it is a string")
                if sp["kind"] == "empty":
                    sp["kind"], data[key] = "block", []
                data[key].append(value)
                sp["end"] = n + 1
                continue
            m = _KEY.match(line)
            if not m:
                raise ValueError("not a `key: value` line (indentation and nested structures are not supported)")
            key = m.group(1)
            if key in data:
                raise ValueError(f"duplicate key: {key}")
            value, comment = _value(m.group(2))
            empty = value == "" and not m.group(2).split("#")[0].strip()
            kind = "flow" if isinstance(value, list) else "empty" if empty else "scalar"
            data[key] = value
            spans[key] = {"start": n, "end": n + 1, "kind": kind, "comment": comment}
        except ValueError as e:
            raise DeskError(f"frontmatter line {n + 2}: {e}: {line.strip()}") from None
    return data, spans


def parse_frontmatter(text: str) -> dict:
    return _parse(split_frontmatter(text)[0])[0]


def frontmatter_comments(text: str) -> dict[str, str]:
    """Trailing comments of key lines, e.g. for spotting a title cut at ` #` by an older writer."""
    return {k: sp["comment"] for k, sp in _parse(split_frontmatter(text)[0])[1].items() if sp["comment"]}


# ---------------------------------------------------------------- write

def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def format_scalar(value, in_flow: bool = False) -> str:
    s = str(value)
    if not s:
        return '""' if in_flow else ""
    plain = (s == s.strip() and s[0] not in _PLAIN_BAD_START and ": " not in s and " #" not in s
             and not s.endswith(":") and all(c >= " " for c in s)
             and not (in_flow and any(c in s for c in ",[]{}")))
    return s if plain else _quote(s)


def format_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(format_scalar(v, in_flow=True) for v in value) + "]"
    return format_scalar(value)


def update_frontmatter(text: str, updates: dict) -> str:
    fm_lines, body = split_frontmatter(text)
    _, spans = _parse(fm_lines)
    out = list(fm_lines)
    # an updated key is written in canonical form (a block list becomes a flow list), keeping
    # the key line's comment (a None value removes the key)
    for key in sorted((k for k in updates if k in spans), key=lambda k: spans[k]["start"], reverse=True):
        sp = spans[key]
        out[sp["start"]:sp["end"]] = ([] if updates[key] is None else
                                      [f"{key}: {format_value(updates[key])}".rstrip() + sp["comment"]])
    out.extend(f"{k}: {format_value(v)}".rstrip() for k, v in updates.items() if k not in spans and v is not None)
    return "---\n" + "\n".join(out) + "\n---\n" + body
