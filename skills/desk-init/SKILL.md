---
name: desk-init
description: 현재 git 저장소를 agent-desk 프로젝트로 등록한다. 사용자가 프로젝트 등록을 요청하거나, 등록되지 않은 저장소에서 작업을 만들려 할 때 사용한다.
---

# agent-desk 프로젝트 등록

CLI 경로는 `agent-desk:desk` 스킬의 CLI 항목을 따른다(이 스킬 base directory 기준 `../../bin/desk.py`).

1. `python <desk.py> where`로 현재 저장소가 이미 등록되었는지 확인한다. `project`가 있으면 중단하고 알린다.
2. 사용자에게 아래 항목을 한 번에 확인한다. 모르면 괄호 안의 기본값을 제안한다.
   - **프로젝트 key**: 사람이 직접 정하는 짧은 이름. 영문·숫자·`_.-`만 사용 (예: `myapp`).
   - **ID 규칙**: 브랜치명에 날짜를 넣는 컨벤션이 있으면 `{branch}`, 없으면 `{yymmdd}-{branch}` (기본). `git branch -a`의 최근 브랜치명을 보고 컨벤션 유무를 판단해 제안한다.
   - **공용 브랜치**: 자동 매칭에서 제외할 브랜치 (기본 `main,master,develop`).
3. 확인받은 값으로 실행한다.
   ```
   python <desk.py> project-init <KEY> --id-template "<규칙>" --shared "main,master"
   ```
4. 출력된 `project.toml` 내용을 사용자에게 보여 준다. 원격 URL과 로컬 경로가 자동으로 기록되며, 다른 PC의 경로는 `paths`에 수동으로 추가할 수 있다고 알린다.
