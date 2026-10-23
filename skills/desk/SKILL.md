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
    ├── state.md      # where things stand: enough to continue from on its own, 60 lines or fewer recommended
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
- **Keep it ready for the next session.** When a meaningful step is finished or the session is wrapping up, bring state.md up to date.
- **Record a decision as soon as it is settled.** Under `## Dn. Title  (YYYY-MM-DD, active)`, with the status words in the records' language as in the template: decision / reason / rejected alternatives / scope. The rejected alternatives and why are what keep the same discussion from repeating. Do not delete a changed decision: add a new entry, then change the old entry's heading status to `superseded → Dm` (`partly superseded → Dm` if only in part). Do not record matters of taste as decisions.
- **Follow the parent task's active decisions.** If you need to depart from one, confirm with the user. Decisions and outcomes that span the whole line of work go in the parent task.
- **Records are a starting point to verify.** When they disagree with the code, the code wins and the record gets fixed. Before proposing a change to an existing design or explaining its reasons, look through decisions.md, closed tasks included, for earlier decisions and rejected alternatives.
- **Repository output never points at the desk.** Do not put task IDs, decision numbers or desk paths in code, docs, commit messages or PRs; spell out the reasons instead. The desk is data only this user sees.
- **Delete records only when the user asks** (task folders, decisions, attachments).
- What is not context for a task (facts that stay true after the task ends, ways of working) does not belong in the desk.
