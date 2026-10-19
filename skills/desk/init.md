# 프로젝트 등록 절차

현재 git 저장소를 agent-desk 프로젝트로 등록한다. CLI는 `SKILL.md`를 따른다.

1. `adesk where`로 현재 저장소가 이미 등록되었는지 확인한다. `project`가 있으면 중단하고 알린다.
2. 사용자에게 아래 항목을 한 번에 확인한다. 모르면 괄호 안의 기본값을 제안한다.
   - **프로젝트 key**: 사람이 직접 정하는 짧은 이름. 영문·숫자·`_.-`만 사용 (예: `myapp`).
   - **공용 브랜치**: 자동 매칭에서 제외할 브랜치 (기본 `main,master,develop`).
3. 확인받은 값으로 실행한다.
   ```
   adesk project-init <KEY> --shared "main,master"
   ```
4. 출력된 `project.toml` 내용을 사용자에게 보여 준다. 원격 URL과 로컬 경로가 자동으로 기록되며, 다른 PC의 경로는 `paths`에 추가할 수 있다고 알린다(요청하면 사본을 고쳐 `adesk put`으로 반영, `SKILL.md` "파일별 작성 규칙").
5. 데이터 폴더(`adesk where`의 `root`)를 사용자 설정(`~/.claude/settings.json`)의 `permissions.additionalDirectories`에 넣으면 기록을 읽을 때 권한 확인이 줄어든다고 한 줄로 안내한다. 이미 들어 있으면 생략한다.
