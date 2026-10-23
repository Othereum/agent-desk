# agent-desk

Claude Code가 어제 하던 일을 기억하게 하는 플러그인이다. 진행상황·결정·발견을 태스크 단위로 기록(데스크)하고, 새 세션에서 그 일을 이어 가면 에이전트가 읽고 시작한다.

## 설계 원칙
- 기능을 추가하거나 동작을 바꿀 때는 docs/design-principles.md의 원칙을 따른다.

## 사용 경험
- README.md(영어)와 README.ko.md(한국어) "사용 방법"에는 핵심 사용 패턴 몇 가지를 대화 예시로 보여 준다. 한쪽을 고치면 다른 쪽도 맞춘다.
- hook 주입, 스킬 절차, CLI 동작을 바꿨는데 남아 있는 예시와 어긋나면, 코드와 예시 중 어느 쪽을 고칠지 사용자에게 확인한다.
- 모든 기능은 사용자가 말로 요청하면 에이전트가 처리할 수 있어야 한다.

## 릴리스
main에 들어가는 것이 곧 릴리스다. 설치한 플러그인은 main의 사본을 쓰고, `.claude-plugin/plugin.json`의 `version`이 바뀌어야 `claude plugin update`가 새 내용을 가져온다. 버전을 그대로 둔 채 main을 바꾸면 같은 버전인데 설치 시점에 따라 내용이 달라진다.

지금은 1.0 이전이다.
- origin/main에 푸시하면 바로 릴리스된다. 푸시는 내보낼 만한 단위가 모였을 때 한 번에 한다.
- 설치한 사용자에게 영향을 주는 변경(bin/, hooks/, skills/, templates/ 등)을 origin/main에 넣을 때는 `version` 올림을 포함한다.
- 버전: breaking change는 minor를, 기능 추가·수정은 patch를 올린다.
- 중간 상태를 원격에 올려야 하는 작업은 브랜치에서 하고, 동작하는 상태가 되면 main에 병합한다.
- 데이터 형식이 바뀌면 이전 형식을 알아보고 변환하는 `adesk migrate`를 함께 고친다.

1.0을 내면 위 규칙을 다음으로 바꾼다.
- 모든 변경은 피처 브랜치에서 작업하고 PR로 main에 squash 병합한다. 변경 기록은 squash 커밋 메시지와 PR 본문으로 남긴다.
- 설치한 사용자에게 영향을 주는 PR은 버전 올림을 포함하고, 병합 커밋에 `vX.Y.Z` 태그를 붙인다. README·CI처럼 영향이 없는 PR은 버전을 올리지 않는다.
- semver: 데이터나 사용 방식이 깨지면 major, 기능 추가는 minor, 수정은 patch. 데이터 형식을 깨는 변경은 migrate와 함께 낸다.

## 테스트
- hook·주입·CLI 코드를 바꾸면 `python -m unittest discover -s tests`로 테스트를 돌린다.
- 스킬, `hooks/hooks.json`, `.claude-plugin/`의 manifest를 바꾸면 `claude plugin validate .`를 돌린다. 루트 CLAUDE.md 경고는 의도한 것이므로 무시하고, `--strict`는 쓰지 않는다.
- 릴리스 전에는 설치한 플러그인으로 등록된 저장소에서 `claude -p "세션 시작 때 받은 [agent-desk] 맥락을 그대로 출력해"`를 한 번 실행해, hooks.json 등록과 플러그인 경로까지 동작하는지 확인한다.

## 문구
- 스킬·주입·CLI·manifest 문구는 영어 하나로 쓴다. 데이터로 남는 템플릿은 `templates/<lang>/`에 언어별로 둔다.
- 코드 문자열, 문서, 커밋 메시지에 특정 서비스(클라우드 저장소)나 사내 프로젝트 이름을 쓰지 않고 일반화한다.
- 스킬·주입·CLI 문구는 배포물이므로 특정 PC의 도구·경로·환경에 기대지 않는다.
