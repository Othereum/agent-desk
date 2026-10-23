# agent-desk

English | [한국어](README.ko.md)

**A plugin that lets Claude Code remember what it was working on yesterday.**

It keeps context by recording progress, decisions and findings per task, and when you continue that work in a new session, Claude reads the record before it starts. You do not have to explain again "how far we got and why we did it this way."

## Installation

> Requires Python 3.11 or later and git.

```
claude plugin marketplace add Othereum/agent-desk
claude plugin install agent-desk@agent-desk
```

## Usage

### A repository you use for the first time

Register a desk folder to keep the repository's records.

```
You     /desk register this repository
Claude  You have a cloud-synced folder, so I suggest this location: `~/XxxDrive/desk/myapp`
        Shall I create the folder there and register the repository?
You     Yes
Claude  Registered this repository with the desk folder `~/XxxDrive/desk/myapp`.
```

### Starting a task

When you are about to start work that is likely to span several sessions, Claude asks whether to record it as a task. You can also say "make this a task" first.
The goal and scope you set at the start stay in the record, so you can continue from the same premises days later.

```
You     I'm going to build login. Only email login this time, leave out social login.
Claude  Shall I record this as a task as we go?
You     Yes
Claude  Created task 261016-login and linked it to the current branch (feature/login).
        Goal: email login (social login out of scope)
```

### Recording a direction as a decision

A direction you settle together is recorded as a decision, along with the rejected alternatives and why.

```
You     Let's refresh the token 5 minutes before it expires. Checking on every request is too heavy.
Claude  Recorded as decision D1.
        Rejected alternative: checking expiry on every request (costs every request)
```

### Ending a session and continuing the next day

When you wrap up a session, Claude brings the task's state up to date.
A new session starts with the list of open tasks, so when you say which work to continue, Claude reads that record and starts.

```
You     Let's stop here for today.
Claude  Updated the state. The login API is done; next is token refresh.
```

```
(the next day, a new session)
You     Let's continue the login work.
Claude  Read the 261016-login record. The login API is done and token refresh is next.
        We decided to refresh 5 minutes before expiry (D1), so I won't add a check to the request path.
```

### Asking why an earlier decision was made

When you ask why something was decided, Claude looks through the decision records, finished tasks included, and answers. You do not have to have the same discussion again from scratch.

```
You     Why did we refresh the token ahead of time instead of checking on every request?
Claude  It was decided in D1 of 261016-login. Checking expiry on every request
        costs every request, so that was rejected.
```

### Finishing a task

When you finish, Claude writes the final outcome into the state and marks the task done. The decisions stay with the task.

```
You     Login is merged. Wrap it up and close the task.
Claude  Wrote the final outcome (email login, refresh 5 minutes before expiry)
        into the state and marked the task done.
```

## Core concepts

The record space agent-desk manages is called the **desk**.

| Name | What it means |
|---|---|
| desk | agent-desk's record space (one desk folder per repository) |
| task record | the state, decisions and log of one task |
| state / decisions / log | state.md / decisions.md / log.md |
| attachments | files attached to a task (files/) |
| task status | open (todo, in progress, on hold), closed (done, cancelled) |

### Tasks

A task is a piece of work with one goal, like "implement login" or "fix the save bug", and all of agent-desk's records are kept per task. Each task keeps three things: **where things stand** (state), **what was decided and why** (decisions), and **what was found** (log).

When a session starts, Claude receives the list of open tasks. You decide what to work on; Claude reads that task's record, starts, and records into it. If you link a task to a branch, the list shows it, so asking to do this branch's work starts with that task.

```
git branch                 agent-desk task
─────────────              ─────────────────────────────────────────────
feature/login   ───▶       261016-login/
                             ├─ state.md       how far it got, what is next
                             ├─ decisions.md   what was decided and why (rejected alternatives included)
                             └─ log.md         what was found
fix/crash       ───▶       261016-crash-on-save/
                ···        tasks without a branch (picked by request)
```

## Data layout

The desk's records are all Markdown files that people can read and edit, kept in a desk folder outside the repository. Claude reads and edits these files directly.

```
<desk folder>/
└─ <id>/                  one task
    ├─ state.md             state (rewritten; frontmatter: title · status · parent task · branches)
    ├─ decisions.md         decisions (entries added)
    ├─ log.md               log (append only)
    └─ files/               attachments
```

Records are written in the language you talk to Claude in. Templates are provided in English and Korean; for other languages Claude translates the English ones.

## Configuration

### Desk folder

Register the desk folder's path in the repository's git config. Ask Claude to register it and it will do it for you. Worktrees share the same setting.

```
git config agent-desk.dir "~/XxxDrive/desk/myapp"
```

A cloud-synced folder is recommended, since it lets several computers use the same records. Register the clones on other computers with the same folder.
- Set the folder's files to always be kept on the device. Otherwise reading them can be slow or fail.
- If the same file is edited on two computers at once and a conflict copy appears, Claude is told about it when a session starts.

## Learn more

- [`desk` skill](skills/desk/SKILL.md): the structure and rules Claude follows when working with the desk
- [Design principles](docs/design-principles.md) (Korean): the principles followed when deciding features and behavior

## License

[MIT](./LICENSE)
