# AIManager

팀의 AI 스킬·플러그인·MCP 서버를 Git 저장소 하나로 관리하고, 팀원의 Claude Code와 Codex에 자동으로 공유합니다. [TeamAI](https://github.com/Tencent/teamai-cli)의 공유 방식을 따르되 스킬·플러그인·MCP·사용 통계만 남긴 가벼운 버전입니다.

- 설치 없이 동작: Python 3.9+ 표준 라이브러리, git, (PR용) gh
- 세션 시작 시 백그라운드로 동기화하고, 도구 호출을 기다리게 하지 않음
- 팀 데이터는 팀 저장소(비공개 권장)에, 코드는 이 저장소에

## 설치 (팀원)

팀 저장소 접근 권한과 GitHub 로그인(`gh auth login`)이 필요합니다.

```sh
git clone https://github.com/SungjinWi99/AIManager.git ~/.aimanager/tool
python3 ~/.aimanager/tool/aimanager.py init https://github.com/<조직>/<팀 저장소>.git
```

- Claude Code(`~/.claude/settings.json`)와 Codex(`~/.codex/hooks.json`)에 훅을 등록하고 `~/.local/bin/aimanager`를 만듭니다.
- Codex는 `/hooks`에서 AIManager 훅을 신뢰(trust)해야 동작합니다.
- AI 도구를 새로 열면 팀 스킬과 `aimanager` 스킬이 보입니다. 이후에는 "이 스킬 팀에 공유해줘"처럼 말로 요청하면 됩니다.

## 팀 저장소 구조

```
skills/<이름>/SKILL.md   팀 스킬
mcp/mcp.json             팀 MCP 서버
aimanager.json           팀 설정, 패키지(Claude 플러그인·npm 도구) 선언
stats (브랜치)            사람별 스킬 사용 기록
```

`mcp/mcp.json`과 `aimanager.json`의 `packages`는 TeamAI의 `mcp/mcp.yaml`, `teamai.yaml`의 `packages`와 같은 필드를 JSON으로 씁니다.

```json
{ "servers": [
  { "name": "notion", "transport": "http", "url": "https://mcp.notion.com/mcp" },
  { "name": "fmt", "transport": "stdio", "command": "npx", "args": ["-y", "@acme/fmt-mcp"],
    "env": { "MODE": "strict" }, "requires": ["npx"], "tools": ["claude"] }
] }
```

```json
{ "team": "my-team",
  "packages": {
    "npm": [{ "name": "eslint", "version": "latest", "global": true }],
    "claude": {
      "marketplaces": [{ "name": "claude-plugins-official", "repo": "anthropics/claude-plugins-official" }],
      "plugins": [{ "name": "code-review@claude-plugins-official" }] } } }
```

## 명령

| 명령 | 하는 일 |
|---|---|
| `aimanager init <URL>` | 팀 저장소 연결, 훅 등록, 첫 동기화 (다시 실행해도 안전) |
| `aimanager pull` | 동기화. 세션 시작 때 자동 실행 |
| `aimanager push <스킬>` | 스킬을 팀 저장소에 PR로 올리기 |
| `aimanager mcp list\|add\|remove` | 팀 MCP 목록, 추가·제거 PR |
| `aimanager packages [add <대상>] [--dry-run]` | 팀 플러그인·npm 도구 설치, 선언 추가 PR |
| `aimanager digest [일수]` | 팀 스킬 사용 통계 (기본 7일) |
| `aimanager doctor` | 설치 상태 점검 |
| `aimanager uninstall [--purge]` | 이 PC에서 제거 |

## 동작 방식

- **스킬**: 팀 스킬을 `~/.claude/skills`, `~/.agents/skills`에 링크합니다. 같은 이름의 개인 스킬이 있으면 개인 스킬이 우선합니다.
- **MCP**: 선언된 서버를 `claude mcp add-json -s user`, `codex mcp add`로 넣고, 선언에서 빠지면 뺍니다. AIManager가 넣은 서버만 관리하고, 이름이 같은 개인 서버는 건드리지 않습니다. Codex는 stdio와 http만 지원합니다(`sse` 제외, 헤더는 `Bearer ${ENV}`만).
- **패키지**: 선언이 바뀌면 다음 프롬프트 때 한 번 안내만 합니다. 설치는 `aimanager packages`로 직접 합니다.
- **사용 통계**: 팀 스킬을 쓴 시각·도구·스킬 이름·세션 ID만 기록합니다(Claude Code: Skill 도구와 `/스킬`, Codex: `$스킬`과 SKILL.md 읽기). 프롬프트나 코드는 기록하지 않습니다. 하루 한 번 `stats` 브랜치에 커밋합니다. 같은 세션의 같은 스킬은 한 번으로 셉니다.

## 새 팀 저장소 만들기 (관리자)

빈 비공개 저장소를 만들고 `skills/`, `mcp/mcp.json`(`{"servers": []}`), `aimanager.json`, 빈 `stats` 브랜치를 올린 뒤 팀원을 Collaborator(Write)로 초대합니다.

## 테스트

```sh
python3 -m unittest discover tests
```
