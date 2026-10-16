# agent-desk

## 릴리스
- 설치한 사용자에게 영향을 주는 변경(bin/, hooks/, skills/, templates/)을 푸시할 때는 `.claude-plugin/plugin.json`의 `version`을 올린다. GitHub로 설치한 플러그인은 설치 시점의 사본을 실행하므로, 버전이 바뀌지 않으면 `claude plugin update`가 새 내용을 가져오지 않을 수 있다.
- 버전은 semver를 따른다: 기능 추가는 minor, 버그 수정·문구 수정은 patch. 데이터 형식이 바뀌면 `desk.toml`의 schema_version도 함께 검토한다.

## 문구
- 코드 문자열, 문서, 커밋 메시지에 특정 동기화 서비스(클라우드 저장소)나 사내 프로젝트 이름을 쓰지 않고 일반화한다(공개 저장소).
