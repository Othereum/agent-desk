"""python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from agent_desk.core import DeskError
from agent_desk.frontmatter import (format_scalar, format_value, frontmatter_comments,
                                    parse_frontmatter, update_frontmatter)


def fm(*lines: str) -> str:
    return "---\n" + "\n".join(lines) + "\n---\nbody\n"


class Parse(unittest.TestCase):
    def test_scalars(self):
        d = parse_frontmatter(fm('a: plain text', 'b: "PR #12"', "c: 'it''s'", 'd:', 'e: ""',
                                 'f: "x\\"y\\\\z\\n"', 'g: 기능: 설명', 'h: https://x.y/a#frag'))
        self.assertEqual(d, {"a": "plain text", "b": "PR #12", "c": "it's", "d": "", "e": "",
                             "f": 'x"y\\z\n', "g": "기능: 설명", "h": "https://x.y/a#frag"})

    def test_comments(self):
        d = parse_frontmatter(fm("# leading", "a: x # one space", "b: y  # two", "", "c: [p, q]  # c",
                                 'd: "q" # after quote', "e:   # only comment"))
        self.assertEqual(d, {"a": "x", "b": "y", "c": ["p", "q"], "d": "q", "e": ""})

    def test_unquoted_hash_is_a_comment(self):
        text = fm("title: PR #12")
        self.assertEqual(parse_frontmatter(text)["title"], "PR")
        self.assertEqual(frontmatter_comments(text), {"title": " #12"})

    def test_flow_lists(self):
        d = parse_frontmatter(fm('a: []', 'b: [x, "y, z", \'w\']', 'c: [x,]', 'd: [ x , y ]'))
        self.assertEqual(d, {"a": [], "b": ["x", "y, z", "w"], "c": ["x"], "d": ["x", "y"]})

    def test_block_lists(self):
        d = parse_frontmatter(fm("links:",
                                 "  - https://a.example/1  # first",
                                 "  - https://a.example/2",
                                 "  # a comment between items",
                                 "  - \"quoted, item\"",
                                 "tags:",
                                 "- col0",
                                 "after: z"))
        self.assertEqual(d, {"links": ["https://a.example/1", "https://a.example/2", "quoted, item"],
                             "tags": ["col0"], "after": "z"})

    def test_errors(self):
        for lines in [("a:", "  b: nested"), ("a: |", "  text"), ("a: &anchor x",), ("a: {k: v}",),
                      ("a: x", "a: y"), ('a: "open',), ('a: "x" tail',), ("a: - x",),
                      ("a:", "  - k: v"), ("a: x", "  continued"), ("not a key",), ("a: [x, [y]]",),
                      ("a: [x y",), ("a: [#x]",), ('a: "bad \\q"',), ("a: x", "- stray")]:
            with self.subTest(lines=lines), self.assertRaises(DeskError):
                parse_frontmatter(fm(*lines))

    def test_error_names_the_file_line(self):
        with self.assertRaisesRegex(DeskError, "3번째 줄"):
            parse_frontmatter(fm("a: x", "  b: y"))


class Format(unittest.TestCase):
    def test_quotes_only_when_needed(self):
        self.assertEqual(format_scalar("기능 X"), "기능 X")
        self.assertEqual(format_scalar("https://x/y"), "https://x/y")
        self.assertEqual(format_scalar("PR #12"), '"PR #12"')
        self.assertEqual(format_scalar("a: b"), '"a: b"')
        self.assertEqual(format_scalar("- x"), '"- x"')
        self.assertEqual(format_scalar('say "hi"'), 'say "hi"')
        self.assertEqual(format_scalar('"hi"'), '"\\"hi\\""')
        self.assertEqual(format_scalar(""), "")
        self.assertEqual(format_value([]), "[]")
        self.assertEqual(format_value(["a", "b, c", ""]), '[a, "b, c", ""]')

    def test_round_trip(self):
        for v in ["PR #12", "a: b", "x:", " lead", "#tag", "[x]", "it's", 'q"q', "back\\slash", "tab\there",
                  "line\nbreak", "{x}", "&a", "%", "기능: 설명 #1", "plain"]:
            with self.subTest(v=v):
                self.assertEqual(parse_frontmatter(fm(f"k: {format_value(v)}"))["k"], v)
                self.assertEqual(parse_frontmatter(fm(f"k: {format_value([v, 'z'])}"))["k"], [v, "z"])


class Update(unittest.TestCase):
    def test_scalar_keeps_comment_and_other_lines(self):
        text = fm("# head", "status: active  # note", "links:", "  - u1  # c1", "title: t")
        out = update_frontmatter(text, {"status": "paused", "title": "PR #12", "new": ["a"]})
        self.assertEqual(out, fm("# head", "status: paused  # note", "links:", "  - u1  # c1",
                                 'title: "PR #12"', "new: [a]"))

    def test_block_list_rewritten_as_flow_list(self):
        text = fm("related:  # keep", "  - a  # item note", "  # between", "  - b", "x: y")
        out = update_frontmatter(text, {"related": ["b", "c, d"]})
        self.assertEqual(out, fm('related: [b, "c, d"]  # keep', "x: y"))
        self.assertEqual(parse_frontmatter(out)["related"], ["b", "c, d"])
        self.assertEqual(update_frontmatter(fm("links:", "  - u1", "x: y"), {"links": []}), fm("links: []", "x: y"))

    def test_other_block_lists_untouched(self):
        text = fm("links:", "  - u1  # c1", "  # between", "  - u2", "status: active")
        self.assertEqual(update_frontmatter(text, {"status": "paused"}),
                         fm("links:", "  - u1  # c1", "  # between", "  - u2", "status: paused"))

    def test_flow_list_and_empty_value(self):
        out = update_frontmatter(fm("branches: []", "parent:", "x: y"), {"branches": ["b"], "parent": "p"})
        self.assertEqual(out, fm("branches: [b]", "parent: p", "x: y"))


if __name__ == "__main__":
    unittest.main()
