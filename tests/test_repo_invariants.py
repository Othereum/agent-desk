"""Static invariants of the repository: skill frontmatter, skill trigger terms, public wording.

python -m unittest discover -s tests
"""

import codecs
import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = sorted((REPO / "skills").glob("*/SKILL.md"))

# Desk terms each skill description must carry (K0038): the always-loaded description is how
# the agent learns what a short request like "현황 보여줘" refers to. Add a term here when a
# skill takes on a new one; remove it only together with the decision that drops the term.
TRIGGER_TERMS = {
    "desk": ["데스크", "태스크 기록", "현황", "결정", "로그"],
    "kn": ["데스크", "지식", "knowledge", "데스크 점검", "doctor"],
    "desk-new": ["태스크"],
    "desk-close": ["지식", "완료", "취소", "아카이브"],
    "desk-init": ["데스크", "프로젝트"],
}

# Names that must not appear in a public repository (K0019): sync services, stored rot13 /
# escaped so this file does not name them either. In-house project names are private:
# list them in AGENT_DESK_FORBIDDEN_WORDS (comma separated) to check them locally.
FORBIDDEN_ROT13 = ["barqevir", "qebcobk", "vpybhq", "tbbtyr qevir", "tbbtyrqevir", "funercbvag",
                   "arkgpybhq", "bjapybhq", "flapguvat", "cpybhq"]
FORBIDDEN_ESCAPED = ["\uc6d0\ub4dc\ub77c\uc774\ube0c", "\ub4dc\ub86d\ubc15\uc2a4",
                     "\uad6c\uae00 \ub4dc\ub77c\uc774\ube0c", "\uc544\uc774\ud074\ub77c\uc6b0\ub4dc"]


def forbidden_words() -> list[str]:
    extra = [w.strip() for w in os.environ.get("AGENT_DESK_FORBIDDEN_WORDS", "").split(",") if w.strip()]
    return [codecs.decode(w, "rot13") for w in FORBIDDEN_ROT13] + FORBIDDEN_ESCAPED + extra


def frontmatter_lines(text: str) -> list[str]:
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        raise AssertionError("frontmatter 없음")
    return m.group(1).splitlines()


def yaml_scalar_problem(value: str) -> str | None:
    """Why a single-line YAML scalar would not read as the plain string intended, or None.

    A strict subset check (no YAML library in the standard library): plain scalars may not
    contain ': ' or ' #' (G0002) or start with an indicator; quoted ones must close and end."""
    if not value:
        return "빈 값"
    if value[0] == '"':
        m = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', value)
        return None if m else "큰따옴표 문자열이 바르게 닫히지 않음"
    if value[0] == "'":
        m = re.fullmatch(r"'((?:[^']|'')*)'", value)
        return None if m else "작은따옴표 문자열이 바르게 닫히지 않음"
    if value[0] in "[]{}&*!|>%@`,#?-:" and not (value[0] in "-?:" and len(value) > 1 and value[1] != " "):
        return f"따옴표 없이 '{value[0]}'로 시작"
    if ": " in value or value.endswith(":"):
        return "따옴표 없는 값에 ': ' (G0002)"
    if " #" in value:
        return "따옴표 없는 값에 ' #' (주석으로 잘림)"
    return None


def skill_meta(path: Path) -> dict:
    meta = {}
    for line in frontmatter_lines(path.read_text(encoding="utf-8")):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z_][\w-]*):(?: (.*))?", line)
        if not m:
            raise AssertionError(f"{path.parent.name}: 읽을 수 없는 줄: {line!r}")
        key, value = m.group(1), (m.group(2) or "").strip()
        if problem := yaml_scalar_problem(value):
            raise AssertionError(f"{path.parent.name}: {key}: {problem}")
        if value[0] == '"':
            value = re.sub(r'\\(.)', r'\1', value[1:-1])
        elif value[0] == "'":
            value = value[1:-1].replace("''", "'")
        meta[key] = value
    return meta


class SkillFrontmatter(unittest.TestCase):
    def test_skills_exist(self):
        self.assertEqual(sorted(p.parent.name for p in SKILLS), sorted(TRIGGER_TERMS))

    def test_frontmatter_parses(self):
        for path in SKILLS:
            with self.subTest(skill=path.parent.name):
                meta = skill_meta(path)
                self.assertEqual(meta.get("name"), path.parent.name)
                self.assertTrue(meta.get("description"))

    def test_description_carries_desk_terms(self):
        for path in SKILLS:
            name = path.parent.name
            with self.subTest(skill=name):
                description = skill_meta(path)["description"]
                missing = [t for t in TRIGGER_TERMS.get(name, []) if t not in description]
                self.assertEqual(missing, [], f"{name} description에 용어가 빠짐 (K0038)")

    def test_scalar_checker(self):
        self.assertIsNone(yaml_scalar_problem("plain text, 괄호(a·b)"))
        self.assertIsNone(yaml_scalar_problem('"사실인 것: 설계 결정"'))
        self.assertEqual(yaml_scalar_problem("사실인 것: 설계 결정"), "따옴표 없는 값에 ': ' (G0002)")
        self.assertIsNotNone(yaml_scalar_problem('"닫히지 않음'))
        self.assertIsNotNone(yaml_scalar_problem("[a, b"))
        self.assertIsNotNone(yaml_scalar_problem("값 # 주석"))


class PublicWording(unittest.TestCase):
    def test_no_service_or_inhouse_names(self):
        files = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO, capture_output=True,
                               check=True).stdout.decode("utf-8").split("\0")
        words = [w.casefold() for w in forbidden_words()]
        found = []
        for rel in filter(None, files):
            try:
                text = (REPO / rel).read_text(encoding="utf-8").casefold()
            except (OSError, UnicodeDecodeError):  # deleted in the work tree, or binary
                continue
            found += [f"{rel}: 금지어 {ascii(codecs.encode(w, 'rot13'))} (rot13, 이스케이프)" for w in words if w in text]
        self.assertEqual(found, [], "공개 저장소에 특정 서비스·사내 이름 (K0019)")


if __name__ == "__main__":
    unittest.main()
