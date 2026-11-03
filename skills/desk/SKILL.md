---
name: desk
description: Reads tasks from the desk, records progress in them, and creates new ones. Use when continuing or starting work, when a direction is settled or a step is finished, and when asked about or explaining the reasons behind an existing design or the alternatives it rejected. Do not use when there is no desk information.
---

# agent-desk

The **desk** is where context (where things stand, decisions, findings) is kept per task as Markdown files, so the next session can pick the work up.

## Registering a repository

A repository's desk folder is registered in `git config agent-desk.dir`. When the user asks to register, create the folder where the user wants it and register it. If the user does not name a location, lightly suggest a cloud-synced folder. Example: `~/XxxDrive/desk/myapp`

## Structure

```
<desk folder>/
└── <task-id>/
    ├── state.md      # where things stand now, kept short so the next session can take it in quickly
    ├── decisions.md  # decisions
    ├── log.md        # findings (causes, confirmed facts, dead ends), append only
    └── files/        # attachments (evidence)
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
  - `branches`: `[a, b]`, the branches where this task's work is done

## Rules

- **Suggest, but the user decides.** When about to start new work with a clear goal that is likely to span sessions, first suggest recording it. Do not ask for a quick question, investigation or one-off fix. Which task a session works on is also set by the user's request; ask when it is unclear. Once set, read that task before starting.
- **Keep state.md a snapshot of now, not a history.** When a meaningful step is finished or the session is wrapping up, rewrite each section for the present instead of appending to it. Keep each item to a line or two.
  - Where things stand says where the work is and what is in progress now. Finished work collapses into at most one summary line; individual finished items, commits and fixes disappear, since git and log.md keep them.
  - Before saving, test every line: if it were gone, would the next session act differently? If not, delete it. Lines listing what was committed or also done, how something was fixed, or which function changed are history.
  - Route what is not state: findings and how a bug was fixed go in log.md, and how to build or run things is not desk material. An option the user rejected or had reverted goes in decisions.md only if it passes the decision test below; otherwise drop it.
  - Open questions holds only questions waiting on the user; leave it empty when there are none.
- **Record a decision as soon as it is settled, but only a real decision; most choices are not.** A decision is a choice between alternatives about direction, structure or behavior that a later session could reasonably reopen or undo. Test: without this entry, would the next session likely propose a rejected alternative again, and would that cost more than a small edit or a quick question? If not, it is not a decision. A user picking one of the options you offered does not by itself make it a decision. These are not decisions:
  - why a bug happened and how it was fixed: log.md
  - implementation details the code already shows (names, signatures, how a value is passed)
  - work order, phases and plans: state.md
  - small UI or behavior details that a few lines can change and whose reason is obvious (layout, sizes, labels, defaults, shortcuts)
  - not building something nobody argued for, and matters of taste

  When unsure, leave it out; record it if the same question comes up again.
- **Keep decision entries short.** Under `## Dn. Title  (YYYY-MM-DD, active)`, with the status words in the records' language as in the template: decision / reason / rejected alternatives / scope. One decision per entry, a few lines in design terms. The rejected alternatives and why are what keep the same discussion from repeating; list only those likely to come up again. Scope names the affected parts, not steps or a full file list; causes and investigation stay in log.md. A choice that refines or details an active decision without reversing it goes into that entry: if you are about to write "refines Dn", edit Dn instead. Do not delete a changed decision: add a new entry, then change the old entry's heading status to `superseded → Dm` (`partly superseded → Dm` if only in part).
- **Follow the parent task's active decisions.** If you need to depart from one, confirm with the user. Decisions and outcomes that span the whole line of work go in the parent task.
- **Records are a starting point to verify.** When they disagree with the code, the code wins and the record gets fixed. Before proposing a change to an existing design or explaining its reasons, look through decisions.md, closed tasks included, for earlier decisions and rejected alternatives.
- **Repository output never points at the desk.** Do not put task IDs, decision numbers or desk paths in code, docs, commit messages or PRs; spell out the reasons instead. The desk is data only this user sees.
- **Delete records only when the user asks** (task folders, decisions, attachments).
- What is not context for a task (facts that stay true after the task ends, ways of working) does not belong in the desk.
