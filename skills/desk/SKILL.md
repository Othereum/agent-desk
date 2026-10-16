---
name: desk
description: agent-desk 작업 기록(state·decisions·log)을 읽고 갱신하는 상시 규칙. 세션 컨텍스트에 "[agent-desk]" 블록이 있을 때, 사용자와 방향·설계를 확정했을 때, 작업 단계가 끝났을 때, 새 브랜치를 만들었을 때, 사용자가 작업 현황·결정·이력의 기록이나 조회를 요청할 때 사용한다.
---

# agent-desk 기록 규칙

에이전트가 작업 단위의 현재 상태, 결정, 히스토리를 세션을 넘어 이어가기 위한 개인용 기록 공간이다.

## CLI

세션 컨텍스트의 `[agent-desk]` 블록에 적힌 `CLI:` 명령을 사용한다. 없으면 이 스킬의 base directory 기준 `../../bin/desk.py`를 `python`으로 실행한다.

```
python <desk.py> where                                # 현재 저장소·브랜치의 프로젝트와 작업
python <desk.py> list [--all]                         # 작업 목록 (--all: 아카이브 포함)
python <desk.py> new --title "제목"                    # 현재 브랜치로 작업 생성
python <desk.py> new --title "제목" --no-branch --slug survey-x
python <desk.py> link <id> [--branch B]               # 작업에 브랜치 추가
python <desk.py> status <id> active|paused
python <desk.py> close <id> done|abandoned [--note "..."]
python <desk.py> reopen <id>
python <desk.py> index                                # 작업·knowledge index.md 재생성
python <desk.py> kn search|list|add|supersede|verify|stale ...   # knowledge (agent-desk:kn 스킬)
```
`--project KEY`와 `--cwd DIR`은 하위 명령 **앞에** 둔다. 출력은 JSON이다.

ID 생성, 브랜치 중복 검사, 상태 전환, 아카이브 이동은 반드시 CLI로 한다. 폴더를 직접 만들거나 옮기지 않는다.

## 저장 구조

```
<root>/projects/<KEY>/project.toml        # 프로젝트 설정 (id_template, shared_branches)
<root>/projects/<KEY>/index.md            # 자동 생성, 직접 수정 금지
<root>/projects/<KEY>/tasks/<id>/state.md       # 현재 상태: 덮어씀
<root>/projects/<KEY>/tasks/<id>/decisions.md   # 결정: 항목 추가
<root>/projects/<KEY>/tasks/<id>/log.md         # 히스토리: 덧붙이기만
<root>/projects/<KEY>/archive/<id>/...    # 종료된 작업
<root>/projects/<KEY>/knowledge/K0001-*.md      # 프로젝트 knowledge
<root>/knowledge/G0001-*.md               # 전역 knowledge
```

## 작업 생성 기준

- 사용자가 지시했을 때, 또는 이 세션에서 **새 브랜치를 만들었을 때만** 만든다. 브랜치를 만든 경우에는 만들기 전에 사용자에게 한 줄로 확인한다.
- 그 외에는 만들지 않는다. 잠깐의 질문, 조사, 일회성 수정마다 작업을 만들면 기록이 소음이 된다.
- 프로젝트가 등록되지 않았으면 `agent-desk:desk-init` 절차를 먼저 안내한다.

## 파일별 작성 규칙

모든 파일은 UTF-8(BOM 없음), LF로 저장한다. 파일 편집은 Edit 도구로 직접 한다.

### state.md (현재 상태, 덮어씀)
- 세션 시작 시 이것만 읽어도 이어서 작업할 수 있게 쓴다. **60줄 이내**를 유지하고, 넘으면 지난 내용을 log.md로 옮긴다.
- 섹션: 목표 / 현재 상태 / 다음 할 일 / 미해결 질문 / 핵심 파일(`경로:줄 — 역할`) / 관련 지식(`- K0012 제목`).
- frontmatter의 `design_doc`에는 Claude Docs 설계 문서 URL을 둔다(아래 "문서 위치 기준").
- 갱신할 때 frontmatter의 `updated`를 오늘 날짜로 바꾼다. 내용을 코드와 대조해 확인했다면 `verified`도 바꾼다.
- 커밋 해시는 적지 않는다(rebase로 바뀐다). 브랜치명과 커밋 메시지로 가리킨다.

### decisions.md (결정, 항목 추가)
- 사용자와 방향을 확정한 **즉시** 기록한다. 나중으로 미루지 않는다.
- 형식: `## Dn. 제목  (YYYY-MM-DD, 유효)` 아래에 결정 / 이유 / 기각한 대안 / 영향 범위.
- 결정이 바뀌면 기존 항목을 지우지 않는다. 상태를 `대체됨 → Dm`으로 바꾸고 새 항목을 추가한다.
- 기각한 대안과 그 이유를 꼭 남긴다. 같은 논의를 반복하지 않게 하는 핵심 정보다.

### log.md (히스토리, 덧붙이기만)
- `## YYYY-MM-DD HH:MM` 아래에 짧은 bullet로 적는다. 기존 항목은 수정하지 않는다.
- 무엇을 했는지보다 **무엇을 알아냈는지**(원인, 확인한 사실, 막힌 지점)를 중심으로 쓴다.

## 갱신 시점

- 결정을 확정했을 때: decisions.md에 즉시 기록. 그 결정이 작업이 끝나도 유효한 코드베이스 설계라면 knowledge 기록도 제안한다(`agent-desk:kn`).
- 함정이나 원인을 밝혀냈을 때: log.md에 기록하고, knowledge 기록을 제안한다.
- 의미 있는 단계가 끝났을 때(원인 규명, 구현 완료, 방향 전환): state.md 갱신 + log.md 항목 추가.
- 사용자가 작업을 마무리하거나 세션을 정리하려 할 때: state.md의 "다음 할 일"이 최신인지 확인.
- 컨텍스트 압축 직후(세션 컨텍스트에 압축 안내가 있을 때): 빠진 내용을 기록.
- 마일스톤(설계 확정, 구현 단계 완료)에서 설계 문서(`design_doc`)가 있으면 decisions.md를 바탕으로 갱신한다.
- 구현이 끝났을 때(PR 전): 아래 "저장소 문서" 기준으로 반영이 필요한지 점검해 제안한다.
- 갱신은 짧게 하고, 사용자에게는 "agent-desk 갱신" 정도로 한 줄만 알린다.

## 문서 위치 기준

| 내용 | 위치 | 쓰는 시점 |
|---|---|---|
| 태스크 단위 설계: 배경, 목표, 대안, 결정, 진행 | Claude Docs 설계 문서 (`state.md`의 `design_doc`) | 설계 단계부터 작업과 함께 |
| 시스템의 현재 구조·기능·데이터 포맷·도메인 | 저장소 문서 (프로젝트 지침이 가리키는 위치, 예: `Docs/`) | 코드를 바꾼 같은 브랜치에서, PR 전 |
| 원자적 사실: 함정, 원인, 방법 | knowledge 항목 (`agent-desk:kn`) | 발견 즉시 |
| 사용자와 일하는 방식 | 에이전트 메모리 | 교정을 받았을 때 |

### Claude Docs 설계 문서
- 아래 중 하나 이상에 해당하면 만들기를 제안하고, 만들기 전에 사용자에게 한 줄로 확인한다.
  - 여러 세션에 걸칠 작업
  - 대안을 비교해 고른 설계 결정이 있음
  - 여러 모듈이나 계층(코드, 데이터, 에셋, 서버/클라)에 걸친 변경
  - 리뷰하거나 공유할 예정
- 문서는 docs 스킬(예: `anthropic-skills:docs`)의 절차로 만들고, URL을 `state.md`의 `design_doc`에 기록한다.
- `decisions.md`가 원본이고 설계 문서는 사람이 읽는 정리본이다. 설계 문서의 각 결정에 `D3`처럼 원본 번호를 표시해 추적할 수 있게 한다.

### 저장소 문서
프로젝트 지침에 문서 기준이 있으면 그것을 우선한다. 없으면 아래를 따른다.
- **기존 문서가 다루는 시스템의 동작을 바꿨으면 규모와 관계없이 그 문서를 갱신한다.** 틀린 문서는 문서가 없는 것보다 해롭다.
- **새 문서**는 아래를 모두 만족할 때만 제안한다.
  1. 프로젝트 고유의 시스템, 데이터 포맷, 도구다. 엔진이나 라이브러리 일반 지식은 해당하지 않는다.
  2. 여러 파일과 계층에 걸쳐 있어서 코드 한 곳만 읽어서는 전체 그림이 잡히지 않는다.
  3. 작업이 끝난 뒤에도 리뷰, 디버깅, 기능 추가 때 반복해서 볼 내용이다.
  4. 문서 색인(예: `Docs/README.md`)에 "언제 보나"를 한 줄로 쓸 수 있다. 이 한 줄이 써지지 않으면 문서로 만들 대상이 아니다.
- 기준에 못 미치면: 파일 하나의 동작은 코드 주석, 사실 하나는 knowledge, 태스크에 묶인 설계는 Claude Docs.
- 저장소 문서는 현재 코드 기준으로 쓴다. 설계 의도, 기각한 대안, 진행 경과는 넣지 않는다.
- 새 문서를 만들면 문서 색인에도 링크를 추가한다. 반영은 코드와 같은 브랜치에 커밋되어야 하므로 PR 전에 제안한다.

## 읽을 때의 태도

- 기록은 **확인할 출발점**이지 사실이 아니다. `verified` 날짜가 오래되었거나 코드와 어긋나면 코드를 우선하고 기록을 고친다.
- log.md는 필요할 때만 검색해서 읽는다. 통째로 읽지 않는다.
- 세션 컨텍스트에 동기화 충돌 사본 경고가 있으면 사용자에게 알리고 병합 여부를 확인한다.

## 종료와 재개

종료는 `agent-desk:desk-close` 절차를 따른다. 잠시 멈출 때는 `status <id> paused`, 다시 시작할 때는 `status <id> active`를 쓴다.
