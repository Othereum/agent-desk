"""Task index.md of a project."""

from __future__ import annotations

from pathlib import Path

from .core import write_text
from .model import Project, Task, get_focus_id, load_tasks


def write_index(project: Project) -> Path:
    tasks = load_tasks(project, include_archive=True)
    open_tasks = sorted((t for t in tasks if not t.archived),
                        key=lambda t: t.meta.get("updated", ""), reverse=True)
    closed = sorted((t for t in tasks if t.archived),
                    key=lambda t: t.meta.get("updated", ""), reverse=True)

    focus_id = (get_focus_id(project) or "").casefold()

    def row(t: Task) -> str:
        status = t.status + (" · 포커스" if not t.archived and t.id.casefold() == focus_id else "")
        cells = [t.id, t.meta.get("title", ""), status, ", ".join(t.branches),
                 t.meta.get("parent", "") or "", t.meta.get("updated", "")]
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    head = "| ID | 제목 | 상태 | 브랜치 | 상위 | 갱신일 |\n|---|---|---|---|---|---|\n"
    text = (f"# {project.key} 태스크 목록\n\n"
            "<!-- desk.py index 가 생성하는 파일입니다. 직접 수정하지 마세요. -->\n\n"
            "## 열린 태스크\n\n" + (head + "\n".join(row(t) for t in open_tasks) if open_tasks else "(없음)") +
            "\n\n## 아카이브\n\n" + (head + "\n".join(row(t) for t in closed) if closed else "(없음)") + "\n")
    path = project.dir / "index.md"
    write_text(path, text)
    return path
