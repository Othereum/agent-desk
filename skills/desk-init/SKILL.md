---
name: desk-init
description: 현재 git 저장소를 agent-desk 프로젝트로 등록한다(데스크에 등록). 사용자가 저장소나 프로젝트 등록을 요청하거나, 등록되지 않은 저장소에서 태스크를 만들려 할 때 사용한다.
---

# agent-desk 프로젝트 등록

CLI 실행 방법(`adesk`)은 `agent-desk:desk` 스킬의 CLI 항목을 따른다(대체 경로: 이 스킬 base directory 기준 `../../bin/desk.py`).

1. `adesk where`로 현재 저장소가 이미 등록되었는지 확인한다. `project`가 있으면 중단하고 알린다.
2. 사용자에게 아래 항목을 한 번에 확인한다. 모르면 괄호 안의 기본값을 제안한다.
   - **프로젝트 key**: 사람이 직접 정하는 짧은 이름. 영문·숫자·`_.-`만 사용 (예: `myapp`).
   - **ID 규칙**: 브랜치명에 날짜를 넣는 컨벤션이 있으면 `{branch}`, 없으면 `{yymmdd}-{branch}` (기본). `git branch -a`의 최근 브랜치명을 보고 컨벤션 유무를 판단해 제안한다.
   - **공용 브랜치**: 자동 매칭에서 제외할 브랜치 (기본 `main,master,develop`).
3. 확인받은 값으로 실행한다.
   ```
   adesk project-init <KEY> --id-template "<규칙>" --shared "main,master"
   ```
4. 출력된 `project.toml` 내용을 사용자에게 보여 준다. 원격 URL과 로컬 경로가 자동으로 기록되며, 다른 PC의 경로는 `paths`에 추가할 수 있다고 알린다(요청하면 사본을 고쳐 `adesk put`으로 반영, `agent-desk:desk` 스킬 "파일별 작성 규칙").
5. 데이터 폴더(`adesk where`의 `root`)를 사용자 설정(`~/.claude/settings.json`)의 `permissions.additionalDirectories`에 넣으면 기록을 읽을 때 권한 확인이 줄어든다고 한 줄로 안내한다. 이미 들어 있으면 생략한다.
