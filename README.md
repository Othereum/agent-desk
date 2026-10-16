# agent-desk

코딩 에이전트(Claude Code)가 작업 단위의 **현재 상태·결정·히스토리**를 세션을 넘어 기록하고 조회하는 개인용 작업 기록 공간입니다.

- 세션을 시작하면 현재 브랜치에 연결된 작업의 `state.md`와 결정 목록이 자동으로 주입됩니다.
- 파일을 건드리면 그 파일에 관련된 knowledge(함정, 설계 결정 등)가 자동으로 주입됩니다.
- 결정은 기각한 대안과 함께 남겨서, 같은 논의를 반복하지 않게 합니다.
- 코드(이 플러그인)와 데이터(작업 기록)를 분리합니다. 데이터는 사용자가 지정한 폴더에만 저장됩니다.

## 요구 사항

- Claude Code
- Python 3.11 이상 (`python` 명령으로 실행 가능해야 함)
- git

## 설치

셸에서:

```
claude plugin marketplace add Othereum/agent-desk
claude plugin install agent-desk@agent-desk
```

Claude Code 세션 안에서:

```
/plugin install agent-desk --marketplace Othereum/agent-desk
```

데이터 폴더는 환경 변수 `AGENT_DESK_ROOT`로 지정합니다. 지정하지 않으면 `~/agent-desk`를 사용합니다. `~/.claude/settings.json`의 `env`에 넣으면 Claude Code 세션에 적용됩니다.

```json
{ "env": { "AGENT_DESK_ROOT": "C:/Users/<you>/<동기화 폴더>/agent-desk" } }
```

클라우드 동기화 폴더에 두면 여러 PC에서 같은 기록을 쓸 수 있습니다. 이때는 폴더의 파일이 **항상 로컬에 내려받아져 있도록**(온디맨드 동기화 해제) 설정하세요. 그렇지 않으면 hook이 파일을 읽을 때 지연되거나 실패할 수 있습니다. 두 PC에서 같은 파일을 동시에 고쳐 동기화 충돌 사본이 생기면 SessionStart와 `doctor`가 알려 줍니다.

### 저장소별 데이터 위치

회사 업무와 개인 프로젝트처럼 기록을 분리하고 싶다면, 특정 저장소에만 다른 데이터 폴더를 지정할 수 있습니다. 그 저장소의 `.claude/settings.local.json`에 `AGENT_DESK_ROOT`를 넣으면, 이 값이 사용자 설정을 덮어씁니다.

```json
// <저장소>/.claude/settings.local.json
{ "env": { "AGENT_DESK_ROOT": "C:/Users/<you>/<개인 동기화 폴더>/agent-desk" } }
```

- 데이터 위치는 **세션 단위**로 정해집니다. 그 저장소에서 연 세션은 hook과 CLI 모두 지정한 폴더를 씁니다.
- 개인 경로가 들어가므로 이 파일은 커밋하지 말고 `.gitignore`에 추가하세요.

## 사용

| 스킬 | 용도 |
|---|---|
| `/agent-desk:desk-init` | 현재 git 저장소를 프로젝트로 등록 (key는 직접 지정) |
| `/agent-desk:desk-new` | 작업 생성 후 현재 브랜치에 연결 |
| `/agent-desk:desk-close` | 작업 종료, knowledge 추출, 아카이브 |
| `agent-desk:desk` | 작업 기록 규칙 (에이전트가 상황에 맞춰 사용) |
| `agent-desk:kn` | knowledge 조회·기록 규칙 (에이전트가 상황에 맞춰 사용) |

## 데이터 구조

```
<AGENT_DESK_ROOT>/
├─ desk.toml                          # schema_version
├─ knowledge/G0001-*.md               # 전역 knowledge (프로젝트 무관, 태그로 매칭)
└─ projects/<KEY>/
    ├─ project.toml                   # 프로젝트 설정
    ├─ index.md                       # 자동 생성
    ├─ focus.txt                      # 포커스 작업 ID (없을 수 있음)
    ├─ tasks/<id>/state.md            # 현재 상태 (덮어씀, 60줄 이내)
    ├─ tasks/<id>/decisions.md        # 결정 (항목 추가, 대체 시 표시)
    ├─ tasks/<id>/log.md              # 히스토리 (덧붙이기만)
    ├─ archive/<id>/...               # 종료된 작업
    └─ knowledge/K0001-*.md           # 프로젝트 knowledge (경로 glob과 태그로 매칭)
```

### knowledge

작업이 끝나도 유효한 **코드베이스와 시스템에 대한 사실**을 항목 하나에 하나씩 기록합니다. 사용자와 일하는 방식은 에이전트 메모리에 두고, knowledge에는 넣지 않습니다.

```markdown
---
id: K0012
title: 세션 토큰 캐시는 설정 리로드 시 초기화되지 않는다
kind: pitfall               # decision | pitfall | finding | howto
paths: ["src/auth/**"]
tags: [auth, session]
sources: [261016-feature__login]
location: inline            # inline | repo:<경로> | claude-docs:<url>
status: valid               # valid | stale(stale_reason) | superseded(superseded_by)
created: 2026-10-16
verified: 2026-10-16 15:30   # 코드와 대조해 확인한 시각 (doctor의 기준점)
---
15줄 이내 요약. 근거 코드 위치를 포함합니다.
```

- 에이전트가 파일을 Read/Edit/Write하면 PostToolUse hook이 그 경로에 해당하는 항목(`paths` glob 일치)을 주입합니다. 같은 항목은 세션당 한 번만 주입하고, 컨텍스트 압축이나 `/clear` 뒤에는 다시 주입합니다. 세션 캐시는 시스템 임시 폴더의 `agent-desk-sessions/`에 두며 7일 지나면 정리합니다.
- 틀린 항목은 지우지 않고 `kn supersede`로 대체합니다.
- `doctor`가 낡은 항목을 찾아냅니다. 확인 시각(`verified`) 이후 `paths`를 바꾼 커밋 수(author 날짜 기준이라 rebase 영향 없음), 사라진 경로, 깨진 참조를 봅니다. git은 tree 비교만 하므로 `blob:none` partial clone에서도 blob을 내려받지 않습니다. 7일 넘게 실행하지 않으면 SessionStart가 실행을 안내합니다.
- 긴 설계 문서는 저장소 문서나 Claude Docs에 두고 `location`으로 가리킵니다.

### project.toml

```toml
key = "myapp"
remotes = ["ssh://git@example.com/org/myapp.git"]   # 원격 URL로 프로젝트 식별
paths = ["C:/src/myapp"]                            # 보조 식별 수단 (PC마다 추가 가능)
id_template = "{yymmdd}-{branch}"                   # {branch} {yymmdd} {yyyymmdd} {slug}
shared_branches = ["main"]                       # 작업에 연결하지 않는 브랜치
lang = "ko"
```

### 작업 ID 규칙

- 브랜치에 연결한 작업: `id_template`으로 만듭니다. 브랜치명에 날짜를 넣는 컨벤션이 있으면 `{branch}`, 없으면 기본값 `{yymmdd}-{branch}`를 권합니다.
- 브랜치 없는 작업: 항상 `{yymmdd}-{slug}`입니다.
- `/`는 `__`로, Windows에서 쓸 수 없는 문자는 `_`로 바꿉니다.
- 같은 ID가 이미 있으면(아카이브 포함) `~2`, `~3`을 붙입니다.
- ID는 생성 후 바뀌지 않습니다. 브랜치가 바뀌면 `link`로 추가 연결합니다.
- 브랜치 하나에는 진행 중(active/paused)인 작업 하나만 연결할 수 있습니다.

### 세션의 작업과 포커스

세션이 다루는 작업은 **현재 브랜치에 연결된 작업 → 프로젝트의 포커스 작업** 순서로 정해집니다.

- 브랜치를 따서 개발하는 프로젝트는 브랜치에 연결된 작업이 그대로 쓰입니다.
- `main`에서만 개발하는 프로젝트는 브랜치 없는 작업(`new --no-branch`)과 포커스로 운영합니다. 공용 브랜치에서 만든 브랜치 없는 작업은 자동으로 포커스가 되고, 종료하면 해제됩니다. 다른 작업으로 넘어갈 때는 `focus <id>`를 씁니다.
- 브랜치가 실제로 하는 일을 더 정확히 보여 주므로 브랜치가 우선합니다. 포커스는 브랜치가 아무것도 알려 주지 않을 때의 대안입니다.
- 포커스는 프로젝트당 하나라서, 같은 저장소에서 여러 세션이 서로 다른 브랜치 없는 작업을 동시에 진행하는 경우는 지원하지 않습니다.

### 상위 작업과 연관 작업

- **상위 작업(`parent`)**: 여러 브랜치로 나뉘는 큰 흐름을 묶습니다. 큰 흐름은 브랜치 없는 작업으로 만들고, 각 브랜치 작업을 `--parent`로 그 아래에 둡니다. 계층은 한 단계만 허용합니다. 하위 작업 세션에는 상위 작업의 목표와 결정 제목이 함께 주입되므로, 브랜치가 달라도 흐름 전체의 결정을 이어받습니다.
- **연관 작업(`related`)**: 후속 작업이나 참고할 이전 작업을 가리키는 단순 목록입니다. 주입하지 않고 ID만 표시합니다.

## CLI

```
python bin/desk.py [--project KEY] [--cwd DIR] <command>
  where | list [--all] | index
  project-init KEY [--id-template T] [--shared a,b] [--lang ko]
  new --title T [--branch B | --no-branch --slug S] [--parent ID] [--related a,b] [--no-focus]
  focus [ID | --clear]
  link ID [--branch B]
  relate ID [--parent P | --no-parent] [--add a,b] [--remove a,b]
  status ID active|paused
  close ID done|abandoned [--note N]
  reopen ID
  kn add --title T --kind decision|pitfall|finding|howto [--paths g1,g2] [--tags a,b]
         [--sources ID] [--location L] [--slug S] [--body B] [--global]
  kn list [--all]
  kn search [TERMS...] [--path FILE] [--all] [--limit N]
  kn supersede ID --by ID
  kn verify ID
  kn stale ID [--reason R]
  doctor [--apply] [--min-commits N]
```

## 라이선스

[MIT](./LICENSE)
