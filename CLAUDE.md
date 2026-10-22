# agent-desk

Claude Code가 어제 하던 일을 기억하게 하는 플러그인이다. 진행상황·결정·발견을 태스크 단위로 기록(데스크)하고, 새 세션에서 그 일을 이어 가면 hook이 기록을 주입해 에이전트가 읽고 시작한다.

## 설계 원칙
- 기능을 추가하거나 동작을 바꿀 때는 docs/design-principles.md의 원칙을 따른다.

## 사용 경험
- README.md "사용 방법"에는 핵심 사용 패턴 몇 가지를 대화 예시로 보여 준다.
- hook 주입, 스킬 절차, CLI 동작을 바꿨는데 남아 있는 예시와 어긋나면, 코드와 예시 중 어느 쪽을 고칠지 사용자에게 확인한다.
- 모든 기능은 사용자가 말로 요청하면 에이전트가 처리할 수 있어야 한다.

## 릴리스
지금은 1.0 이전이다. 1.0 이후 규칙과 이유는 CONTRIBUTING.md에 있다.
- origin/main에 푸시하면 바로 릴리스된다.
- 설치한 사용자에게 영향을 주는 변경을 origin/main에 넣을 때는 `.claude-plugin/plugin.json`의 `version` 올림을 포함한다.
- 버전: breaking change는 minor를, 기능 추가·수정은 patch를 올린다.
- 데이터 형식이 바뀌면 `desk.toml`의 schema_version을 검토한다.

## 테스트
- hook·주입·CLI 코드를 바꾸면 `python -m unittest discover -s tests`로 테스트를 돌린다.
- 스킬, `hooks/hooks.json`, `.claude-plugin/`의 manifest를 바꾸면 `claude plugin validate .`를 돌린다. 루트 CLAUDE.md 경고는 의도한 것이므로 무시한다.
- 회귀 테스트 결과 주입 기준(tests/snapshots/)과 다르게 나오고 그게 의도한 변경이면 `AGENT_DESK_UPDATE_SNAPSHOTS=1`로 기준을 갱신하고 diff를 검토한다.

## 문구
- 코드 문자열, 문서, 커밋 메시지에 특정 서비스(클라우드 저장소)나 사내 프로젝트 이름을 쓰지 않고 일반화한다.
- 스킬·주입·CLI 문구는 배포물이므로 특정 PC의 도구·경로·환경에 기대지 않는다.
