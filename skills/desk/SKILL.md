---
name: desk
description: Reads tasks from the desk, records progress in them, and creates new ones. Use when continuing or starting work, when a direction is settled or a step is finished, when asked about or explaining the reasons behind an existing design or the alternatives it rejected, and when the user wants to look through the tasks. Do not use when there is no desk information.
---

# agent-desk

The **desk** is where context (where things stand, decisions, findings) is kept per task as Markdown files, so the next session can pick the work up.

## Registering a repository

A repository's desk folder is registered in `git config agent-desk.dir`. When the user asks to register, create the folder where the user wants it and register it. If the user does not name a location, lightly suggest a cloud-synced folder. Example: `~/XxxDrive/desk/myapp`

## Showing the desk to the user

When the user wants to look through the tasks themselves, run `adesk view`. It opens a live, read-only page in the browser with every desk in the folder that holds this repository's desk, and returns at once. To open it without Claude Code, `adesk view --shortcut menu|desktop|both [FOLDER] [--name NAME]` adds an entry to the start menu (app launcher), the desktop or both, one per folder of desks; ask which the user wants if they did not say. To remove one, delete it: `Agent Desk (...).lnk` on Windows, `Agent Desk (...).app` on macOS, `agent-desk-*.desktop` elsewhere, in the start menu Programs folder (`~/Applications`, `~/.local/share/applications`) or on the desktop.

## Structure

```
<desk folder>/
└── <task-id>/
    ├── state.md      # where things stand now, kept short so the next session can take it in quickly
    ├── decisions.md  # decisions
    ├── log.md        # what happened that nothing else keeps: findings, work outside the repository
    └── files/        # attachments: evidence, and documents too long for state.md (detailed plans, designs)
```

- A task ID is `<yymmdd>-<slug>`.
- The templates for each file are in `../../templates/<lang>/`, relative to this skill's base directory.
- Write records in the user's conversation language; when a task already has records, follow their language. If there is no `templates/<lang>/` for that language, translate the `en` templates.
- The top of state.md describes the task as frontmatter.
  - `title`: the task title
  - `status`: the task status
    - open tasks: `todo`, `in_progress`, `on_hold`
    - closed tasks: `done`, `cancelled`
  - `parent`: the parent task ID (one level only)
  - `branches`: `[a, b]`, the branches where this task's work is done; list only branches dedicated to this task, never shared ones such as the default branch

## Creating tasks

- **Create a task only on the user's explicit instruction or approval.**
- **Suggest recording work that deserves a task, before starting it.** Do not ask for a quick question, investigation or one-off fix.
- **What deserves a task.** A task holds context that would otherwise be lost between sessions. It deserves one when at least one of these holds:
  - The work can't finish in one session: its stages, measurements or prototypes decide what comes next.
  - It waits on something outside the session (an upstream PR, a release, another person) and has to be picked up later.
  - Findings, dead ends or rejected alternatives are building up that a later session would otherwise have to rediscover.

  These are not tasks: work one session can finish, even if it is put off to the next session; a plan that fits in a short note; a to-do list.
- **Carry the conversation into a new task.** A task is usually created right after the discussion that led to it, and the next session gets only what is written down. When creating one, move what the discussion produced into its files: the goal and why the work is needed into Goal, any plan into Stages, directions already settled into decisions.md (if they pass the decision test), and findings and dead ends into log.md. A task holding only a title and a one-line goal makes the next session redo that discussion.
- Set `status` to `in_progress` if work starts now, otherwise `todo`. If the work belongs under an open task, ask the user whether to make it a child and set `parent`.
- What is not context for a task (facts that stay true after the task ends, ways of working) does not belong in the desk.

## Working on a task

- **Read a task before working on it.**
- **Keep `status` in step with the work.** When you start working on a `todo` or `on_hold` task, set it to `in_progress` before anything else. When the work stops to wait or ends, change it then: `on_hold`, or `done`/`cancelled` once the user agrees it is over.
- **A question or an open discussion is not a settlement.** Record when the user agrees to a direction or the discussion moves on, once for the whole thread, not after every answer. This applies wherever these rules say to record something as soon as it is settled or right away.
- **Records are a starting point to verify.** When they disagree with the code, the code wins and the record gets fixed. Before proposing a change to an existing design or explaining its reasons, look through decisions.md, closed tasks included, for earlier decisions and rejected alternatives.
- **Repository output never points at the desk.** Do not put task IDs, decision numbers or desk paths in code, docs, commit messages or PRs; spell out the reasons instead. The desk is data only this user sees.
- **Delete records only when the user asks** (task folders, decisions, attachments).

## state.md

- **Keep state.md a snapshot of now, not a history.** When a meaningful step is finished or the session is wrapping up, rewrite each section for the present instead of appending to it. Keep each item to a line or two.
  - Where things stand says where the work is and what is in progress now. Finished work collapses into at most one summary line; individual finished items, commits and fixes disappear, since git and log.md keep them.
  - Stages holds the whole plan of a task that runs in stages, one line per stage. Mark finished stages done instead of deleting them, since they show the plan's shape, and mark the current one. Next holds only what to do now in the current stage. When the user and you work out a plan, record it here right away, before it exists only in the conversation. A stage that itself spans several sessions is better as a child task.
  - A detailed plan or design too long for state.md goes in a Markdown file under files/, linked from Stages. It says what to build; why, and the rejected alternatives, stay only in decisions.md.
  - Before saving, test every line outside Stages: if it were gone, would the next session act differently? If not, delete it. Lines listing what was committed or also done, how something was fixed, or which function changed are history.
  - Route what is not state: findings and work done outside the repository go in log.md, and how to build or run things is not desk material. An option the user rejected or had reverted goes in decisions.md only if it passes the decision test below; otherwise drop it.
  - Open questions holds only questions waiting on the user; leave it empty when there are none.

## decisions.md

- **Record a decision as soon as it is settled, but only a real decision; most choices are not.** A decision is a choice between alternatives about direction, structure or behavior that a later session could reasonably reopen or undo. Test: without this entry, would the next session likely propose a rejected alternative again, and would that cost more than a small edit or a quick question? If not, it is not a decision. A user picking one of the options you offered does not by itself make it a decision. These are not decisions:
  - why a bug happened: log.md (how it was fixed is in the code)
  - implementation details the code already shows (names, signatures, how a value is passed)
  - work order, stages and plans: Stages in state.md, details in a plan file under files/
  - small UI or behavior details that a few lines can change and whose reason is obvious (layout, sizes, labels, defaults, shortcuts)
  - not building something nobody argued for, and matters of taste

  When unsure, leave it out; record it if the same question comes up again.
- **Keep decision entries short.** Under `## Dn. Title  (YYYY-MM-DD, active)`, with the status words in the records' language as in the template: decision / reason / rejected alternatives / scope. One decision per entry, a few lines in design terms. The rejected alternatives and why are what keep the same discussion from repeating; list only those likely to come up again. Scope names the affected parts, not steps or a full file list; causes and investigation stay in log.md. A choice that refines or details an active decision without reversing it goes into that entry: if you are about to write "refines Dn", edit Dn instead. Do not delete a changed decision: add a new entry, then change the old entry's heading status to `superseded → Dm` (`partly superseded → Dm` if only in part).
- **Follow the parent task's active decisions.** If you need to depart from one, confirm with the user. Decisions and outcomes that span the whole line of work go in the parent task.

## log.md

- **Record only what no other record keeps.** log.md holds what happened that code, git and the other desk files do not, and that the next session would otherwise have to rediscover or re-check at a cost:
  - findings: causes, confirmed facts, dead ends and why they failed
  - work done outside the repository and its results (data migrations, server or config changes, measurements, issues filed upstream)
  - where the work landed when git cannot tell: commits or PRs on a shared branch, other repositories

  Do not copy what git already has: status changes, "committed", "tests passed", a diff retold in words.
- **Add entries at the bottom under a date heading.**
- **Keep every entry true on its own; fix a wrong entry in place instead of adding a correction.** A reader should be able to trust any single entry without reading on to find it was overturned. Also fix entries that drew conclusions from it. If the wrong reading is a trap a later session could fall into again, keep it as one line in the fixed entry ("looked like X at first, but Y").
