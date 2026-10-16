# agent-desk

## 릴리스
지금은 1.0 이전이다. 1.0 이후 규칙과 이유는 CONTRIBUTING.md에 있다.
- main에 푸시하는 것이 곧 릴리스다. 설치한 사용자에게 영향을 주는 변경(bin/, hooks/, skills/, templates/ 등)을 푸시할 때는 그 푸시에 `.claude-plugin/plugin.json`의 `version` 올림을 포함한다.
- 푸시는 내보낼 만한 단위가 모였을 때 한 번에 한다. 중간 상태를 원격에 올려야 하면 브랜치를 쓴다.
- semver를 따른다: 기능 추가·깨는 변경은 minor, 수정은 patch. 깨지는지는 변경할 때 판단한다.
- 데이터 형식이 바뀌면 `desk.toml`의 schema_version을 검토한다.

## 문구
- 코드 문자열, 문서, 커밋 메시지에 특정 동기화 서비스(클라우드 저장소)나 사내 프로젝트 이름을 쓰지 않고 일반화한다(공개 저장소).
