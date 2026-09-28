#!/usr/bin/env python3
"""AIManager: 팀의 AI 스킬·플러그인·MCP를 Claude Code와 Codex에 공유한다.

팀 저장소(데이터)와 이 도구(코드)는 분리되어 있다. 팀 저장소 구조:
  skills/<이름>/SKILL.md   팀 스킬
  mcp/mcp.json             팀 MCP 서버 선언
  aimanager.json           팀 설정과 패키지(Claude 플러그인, npm 도구) 선언
  stats 브랜치             사람별 스킬 사용 기록(<사용자>.tsv)

Python 3.9 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".aimanager"
TEAM = ROOT / "team"
STATE = ROOT / "state"
TOOL = Path(__file__).resolve().parent
SCRIPT = TOOL / "aimanager.py"
# Claude Code는 ~/.claude/skills, Codex는 ~/.agents/skills를 읽는다.
SKILL_TARGETS = [HOME / ".claude" / "skills", HOME / ".agents" / "skills"]
HOOK_FILES = {"claude": HOME / ".claude" / "settings.json", "codex": HOME / ".codex" / "hooks.json"}
# (이벤트, matcher). Claude는 Skill 도구, Codex는 셸로 SKILL.md를 읽는 것을 본다.
HOOK_EVENTS = {
    "claude": [("SessionStart", "startup"), ("UserPromptSubmit", None), ("PostToolUse", "Skill")],
    "codex": [("SessionStart", "startup"), ("UserPromptSubmit", None), ("PostToolUse", "Bash")],
}
EVENT_ARG = {"SessionStart": "session-start", "UserPromptSubmit": "prompt-submit", "PostToolUse": "post-tool-use"}
EMPTY_PACKAGES = {"npm": [], "claude": {"marketplaces": [], "plugins": []}}


# ---------- 공통 ----------

def git(*args: str, cwd: Path = TEAM, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def config() -> dict:
    return load_json(STATE / "config.json", {})


def team_skills() -> dict[str, Path]:
    """링크할 스킬: 도구 기본 스킬 + 팀 스킬 (이름이 같으면 팀 스킬)."""
    found = {}
    for base in (TOOL / "skills", TEAM / "skills"):
        for d in sorted(base.glob("*/SKILL.md")):
            found[d.parent.name] = d.parent
    return found


def is_ours(link: Path) -> bool:
    target = os.readlink(link)
    return target.startswith(str(TEAM) + os.sep) or target.startswith(str(TOOL) + os.sep)


def repo_web_url() -> str:
    url = git("remote", "get-url", "origin").stdout.strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return f"https://github.com/{m.group(1)}" if m else url


# ---------- 스킬 링크 ----------

def link_skills() -> None:
    skills = team_skills()
    for target in SKILL_TARGETS:
        target.mkdir(parents=True, exist_ok=True)
        for name, src in skills.items():
            link = target / name
            if link.is_symlink():
                if not is_ours(link):
                    continue  # 개인이 직접 건 링크는 건드리지 않는다
                link.unlink()
            elif link.exists():
                continue  # 같은 이름의 개인 스킬이 우선한다
            link.symlink_to(src)
        for link in target.iterdir():  # 팀에서 삭제된 스킬의 링크 정리
            if link.is_symlink() and is_ours(link) and not link.exists():
                link.unlink()


# ---------- 사용 기록 ----------

def skills_used(event: dict) -> set[str]:
    """훅 입력에서 팀 스킬 이름을 찾는다. 프롬프트·명령 원문은 저장하지 않는다."""
    names = set()
    tool_input = event.get("tool_input") or {}
    if event.get("tool_name") == "Skill":
        names.add(str(tool_input.get("skill", "")))
    prompt = event.get("prompt") or ""
    m = re.match(r"\s*/([\w-]+)", prompt)  # Claude Code: /스킬
    if m:
        names.add(m.group(1))
    names.update(re.findall(r"(?<![\w$])\$([\w-]+)", prompt))  # Codex: $스킬
    names.update(re.findall(r"skills/([\w-]+)/SKILL\.md", json.dumps(tool_input)))  # 셸로 SKILL.md 읽기
    return names & set(team_skills())


def record_usage(event: dict, tool: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sid = event.get("session_id") or "-"
    lines = "".join(f"{now}\t{tool}\t{n}\t{sid}\n" for n in sorted(skills_used(event)))
    if lines:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "usage.tsv", "a") as f:
            f.write(lines)


def stats_clone() -> Path | None:
    s = STATE / "stats"
    if not (s / ".git").exists():
        url = git("remote", "get-url", "origin").stdout.strip()
        r = subprocess.run(["git", "clone", "-q", "--branch", "stats", "--single-branch", url, str(s)], capture_output=True)
        if r.returncode:
            return None
    return s if git("pull", "-q", "--rebase", cwd=s).returncode == 0 else None


def report_usage() -> None:
    """하루 한 번, 쌓인 기록을 stats 브랜치의 <사용자>.tsv에 붙여 push한다."""
    today = dt.date.today().isoformat()
    usage, sending, reported = STATE / "usage.tsv", STATE / "usage.sending", STATE / "reported"
    if reported.exists() and reported.read_text().strip() == today:
        return
    if not usage.exists() or not usage.stat().st_size:
        return
    s = stats_clone()
    if not s:
        return
    usage.rename(sending)  # 보고 중에 새로 쌓이는 기록을 잃지 않도록 먼저 옮긴다
    user = config()["user"]
    with open(s / f"{user}.tsv", "a") as f:
        f.write(sending.read_text())
    git("add", f"{user}.tsv", cwd=s)
    ok = git("commit", "-qm", f"stats: {user} {today}", cwd=s).returncode == 0 and (
        git("push", "-q", cwd=s).returncode == 0
        or (git("pull", "-q", "--rebase", cwd=s).returncode == 0 and git("push", "-q", cwd=s).returncode == 0)
    )
    if ok:
        sending.unlink()
        reported.write_text(today + "\n")
    else:
        git("reset", "-q", "--hard", "@{u}", cwd=s)
        with open(usage, "a") as f:
            f.write(sending.read_text())
        sending.unlink()


# ---------- MCP ----------

def mcp_declared() -> list[dict]:
    return load_json(TEAM / "mcp" / "mcp.json", {"servers": []}).get("servers", [])


def claude_mcp_names() -> set[str]:
    return set(load_json(HOME / ".claude.json", {}).get("mcpServers", {}))


def codex_mcp_names() -> set[str]:
    try:
        text = (HOME / ".codex" / "config.toml").read_text()
    except OSError:
        return set()
    return set(re.findall(r'^\[mcp_servers\."?([^"\].]+)"?\]', text, re.M))


def mcp_add_cmd(tool: str, s: dict) -> list[str] | None:
    """팀 MCP 선언(TeamAI mcp.yaml과 같은 필드)을 각 도구의 공식 CLI 명령으로 바꾼다. 지원하지 않으면 None."""
    t = s.get("transport", "stdio")
    if tool == "claude":
        if t == "stdio":
            spec = {"type": "stdio", "command": s["command"], "args": s.get("args", []), "env": s.get("env", {})}
        else:
            spec = {"type": t, "url": s["url"], "headers": s.get("headers", {})}
        return ["claude", "mcp", "add-json", "-s", "user", s["name"], json.dumps(spec)]
    if t == "stdio":
        env = [a for k, v in s.get("env", {}).items() for a in ("--env", f"{k}={v}")]
        return ["codex", "mcp", "add", s["name"], *env, "--", s["command"], *s.get("args", [])]
    if t == "http":
        cmd = ["codex", "mcp", "add", s["name"], "--url", s["url"]]
        headers = s.get("headers", {})
        m = re.fullmatch(r"Bearer \$\{(\w+)\}", headers.get("Authorization", ""))
        if m:
            cmd += ["--bearer-token-env-var", m.group(1)]
        elif headers:
            return None  # Codex CLI는 Bearer 토큰 환경 변수 외의 헤더를 받지 않는다
        return cmd
    return None  # Codex는 sse를 지원하지 않는다


def apply_mcp(verbose: bool = False) -> None:
    """선언된 서버를 각 도구에 넣고, 선언에서 빠진 서버는 뺀다. 직접 추가한 서버는 건드리지 않는다."""
    managed_path = STATE / "managed-mcp.json"
    managed = load_json(managed_path, {})
    existing = {"claude": claude_mcp_names, "codex": codex_mcp_names}
    for tool in ("claude", "codex"):
        if not shutil.which(tool):
            continue
        mine = managed.setdefault(tool, {})
        have = existing[tool]()
        wanted = {}
        for s in mcp_declared():
            if tool not in s.get("tools", ["claude", "codex"]):
                continue
            if any(not shutil.which(r) for r in s.get("requires", [])):
                continue
            wanted[s["name"]] = s
        for name in list(mine):
            if name not in wanted or mine[name] != _hash(wanted[name]):
                subprocess.run([tool, "mcp", "remove", *(["-s", "user"] if tool == "claude" else []), name], capture_output=True)
                del mine[name]
        for name, s in wanted.items():
            if name in mine:
                continue
            if name in have:
                if verbose:
                    print(f"  {tool}: '{name}' 이름의 서버가 이미 있어 건너뜀")
                continue
            cmd = mcp_add_cmd(tool, s)
            try:
                added = bool(cmd) and subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=60).returncode == 0
            except subprocess.TimeoutExpired:
                added = False
            if added:
                mine[name] = _hash(s)
            elif verbose:
                print(f"  {tool}: '{name}' 추가 실패 또는 미지원")
    save_json(managed_path, managed)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]


# ---------- 패키지 ----------

def packages_declared() -> dict:
    return load_json(TEAM / "aimanager.json", {}).get("packages", EMPTY_PACKAGES)


def package_cmds(p: dict) -> list[list[str]]:
    cmds = [["claude", "plugin", "marketplace", "add", m["repo"]] for m in p["claude"]["marketplaces"]]
    cmds += [["claude", "plugin", "install", pl["name"]] for pl in p["claude"]["plugins"]]
    for n in p["npm"]:
        cmd = ["npm", "install", "-g", f"{n['name']}@{n.get('version', '*')}"]
        if n.get("registry"):
            cmd += ["--registry", n["registry"]]
        cmds.append(cmd)
    return cmds


def check_packages_notice() -> None:
    """패키지 선언이 바뀌면 다음 프롬프트 때 한 번 안내한다. 자동으로 설치하지 않는다."""
    p = packages_declared()
    h = _hash(p)
    if not package_cmds(p) or h in (_read(STATE / "packages-ack"), _read(STATE / "packages-notified")):
        return
    (STATE / "notice").write_text(
        "[AIManager] 팀 플러그인·패키지 선언이 바뀌었습니다. 자동으로 설치하지 말고, "
        "사용자에게 `aimanager packages`로 확인 후 설치하도록 한 줄로 안내하세요.\n")
    (STATE / "packages-notified").write_text(h)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


# ---------- PR ----------

def open_pr(branch: str, title: str, change) -> None:
    """origin/main에서 새 worktree를 만들어 change(worktree)를 적용하고 PR을 연다."""
    git("fetch", "-q", "origin", check=True)
    wt = Path(tempfile.mkdtemp()) / "wt"
    git("worktree", "add", "-q", "-b", branch, str(wt), "origin/main", check=True)
    try:
        change(wt)
        git("add", "-A", cwd=wt)
        if not git("status", "--porcelain", cwd=wt).stdout.strip():
            print("바뀐 내용이 없어 PR을 만들지 않았습니다.")
            return
        git("commit", "-qm", title, cwd=wt, check=True)
        git("push", "-q", "-u", "origin", branch, cwd=wt, check=True)
        r = subprocess.run(["gh", "pr", "create", "--fill", "--head", branch], cwd=wt, capture_output=True, text=True) \
            if shutil.which("gh") else None
        print(r.stdout.strip() if r and r.returncode == 0 else f"PR 만들기: {repo_web_url()}/compare/main...{branch}?expand=1")
    finally:
        git("worktree", "remove", "--force", str(wt))


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d%H%M%S")


def edit_json(rel: str, default, fn):
    def change(wt: Path):
        path = wt / rel
        data = load_json(path, default)
        fn(data)
        save_json(path, data)
    return change


# ---------- 명령 ----------

def cmd_init(a) -> None:
    ROOT.mkdir(exist_ok=True)
    if not (TEAM / ".git").exists():
        subprocess.run(["git", "clone", "-q", a.repo, str(TEAM)], check=True)
    cfg = config()
    cfg["repo"] = a.repo
    if not cfg.get("user"):
        user = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True).stdout.strip() \
            if shutil.which("gh") else ""
        user = user or git("config", "user.name").stdout.strip() or os.environ.get("USER", "user")
        cfg["user"] = re.sub(r"[^\w.-]", "_", user)
    save_json(STATE / "config.json", cfg)
    register_hooks()
    bin_link = HOME / ".local" / "bin" / "aimanager"
    bin_link.parent.mkdir(parents=True, exist_ok=True)
    if bin_link.is_symlink():
        bin_link.unlink()
    if not bin_link.exists():
        bin_link.symlink_to(SCRIPT)
    SCRIPT.chmod(0o755)
    cmd_pull(argparse.Namespace(quiet=False))
    print(f"\n완료. 사용자: {cfg['user']}")
    if str(bin_link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"  {bin_link.parent} 를 PATH에 추가하면 어디서든 `aimanager`를 쓸 수 있습니다.")
    print("  Codex를 쓴다면 Codex에서 /hooks 를 열고 AIManager 훅을 신뢰(trust)해 주세요.")
    print("  AI 도구를 새로 열면 팀 스킬이 보입니다.")


def hook_cmd(event: str, tool: str) -> str:
    return f'"{sys.executable}" "{SCRIPT}" hook {EVENT_ARG[event]} {tool}'


def strip_our_hooks(hooks: dict) -> None:
    for name in list(hooks):
        hooks[name] = [g for g in hooks[name] if not any(str(SCRIPT) in h.get("command", "") for h in g.get("hooks", []))]
        if not hooks[name]:
            del hooks[name]


def register_hooks() -> None:
    for tool, path in HOOK_FILES.items():
        cfg = load_json(path, {})
        hooks = cfg.setdefault("hooks", {})
        strip_our_hooks(hooks)  # 다시 실행해도 중복되지 않게 먼저 지운다
        for event, matcher in HOOK_EVENTS[tool]:
            group = {"hooks": [{"type": "command", "command": hook_cmd(event, tool)}]}
            if matcher:
                group["matcher"] = matcher
            hooks.setdefault(event, []).append(group)
        save_json(path, cfg)


def cmd_pull(a) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    git("pull", "-q", "--ff-only", cwd=TOOL)
    git("pull", "-q", "--ff-only")
    link_skills()
    apply_mcp(verbose=not a.quiet)
    check_packages_notice()
    report_usage()
    if not a.quiet:
        print("팀 스킬:", ", ".join(sorted(team_skills())) or "(없음)")


def cmd_hook(a) -> None:
    """에이전트 훅 진입점. 실패해도 에이전트를 막지 않고, 안내문 외에는 출력하지 않는다."""
    try:
        if a.event == "session-start":
            subprocess.Popen([sys.executable, str(SCRIPT), "pull", "--quiet"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
        event = json.loads(sys.stdin.read() or "{}")
        record_usage(event, a.tool)
        notice = STATE / "notice"
        if a.event == "prompt-submit" and notice.exists():
            print(notice.read_text().strip())
            notice.unlink()
    except Exception:
        pass


def resolve_skill(arg: str) -> Path:
    p = Path(arg).expanduser()
    candidates = [p] if p.exists() else [t / arg for t in SKILL_TARGETS]
    for c in candidates:
        if (c / "SKILL.md").exists():
            return c.resolve()
    sys.exit(f"스킬을 찾을 수 없습니다: {arg}")


def cmd_push(a) -> None:
    src = resolve_skill(a.skill)
    name = src.name
    in_team = str(src).startswith(str(TEAM) + os.sep)

    def change(wt: Path):
        dst = wt / "skills" / name
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store", ".git"))

    git("fetch", "-q", "origin")
    new = not git("ls-tree", "--name-only", "origin/main", f"skills/{name}").stdout.strip()
    open_pr(f"share/{name}-{stamp()}", f"{'feat' if new else 'fix'}(skills): {name}", change)
    if in_team:  # 팀 사본을 직접 고쳤다면 되돌려야 다음 동기화가 멈추지 않는다
        git("checkout", "--", f"skills/{name}")
        git("clean", "-fdq", f"skills/{name}")


def cmd_packages(a) -> None:
    p = packages_declared()
    if a.action == "add":
        def add(data):
            pk = data.setdefault("packages", json.loads(json.dumps(EMPTY_PACKAGES)))
            t = a.target
            if a.npm or t.startswith("@") or "@" not in t:  # 플러그인은 이름@마켓플레이스
                name, ver = t.rsplit("@", 1) if "@" in t.lstrip("@") else (t, "*")
                pk["npm"].append({"name": name, "version": ver, "global": True})
            else:
                mkt = a.target.split("@", 1)[1]
                repo = a.marketplace_repo or ("anthropics/claude-plugins-official" if mkt == "claude-plugins-official" else None)
                if not repo and not any(m["name"] == mkt for m in pk["claude"]["marketplaces"]):
                    sys.exit(f"마켓플레이스 '{mkt}'의 저장소를 --marketplace-repo owner/repo 로 알려주세요. npm 패키지라면 --npm 을 붙이세요.")
                if repo and not any(m["name"] == mkt for m in pk["claude"]["marketplaces"]):
                    pk["claude"]["marketplaces"].append({"name": mkt, "repo": repo})
                pk["claude"]["plugins"].append({"name": a.target})
        open_pr(f"packages/{stamp()}", f"chore(packages): {a.target} 추가", edit_json("aimanager.json", {}, add))
        return
    cmds = package_cmds(p)
    if not cmds:
        print("선언된 패키지가 없습니다.")
        return
    for cmd in cmds:
        print("$", " ".join(cmd))
        if not a.dry_run and subprocess.run(cmd).returncode:
            sys.exit("설치 실패. 위 명령을 확인하세요.")
    if not a.dry_run:
        (STATE / "packages-ack").write_text(_hash(p))


def cmd_mcp(a) -> None:
    if a.action == "list":
        managed = load_json(STATE / "managed-mcp.json", {})
        for s in mcp_declared():
            where = [t for t in ("claude", "codex") if s["name"] in managed.get(t, {})]
            print(f"{s['name']}\t{s.get('transport', 'stdio')}\t적용: {', '.join(where) or '-'}")
        return
    if a.action == "remove":
        def rm(data):
            data["servers"] = [s for s in data.get("servers", []) if s["name"] != a.name]
        open_pr(f"mcp/{stamp()}", f"chore(mcp): {a.name} 제거", edit_json("mcp/mcp.json", {"servers": []}, rm))
        return
    s = {"name": a.name}
    if a.url:
        s.update(transport=a.transport or "http", url=a.url)
        if a.header:
            s["headers"] = dict(h.split("=", 1) for h in a.header)
    elif a.command:
        s.update(transport="stdio", command=a.command[0], args=a.command[1:])
    else:
        sys.exit("--url 또는 `-- 명령 인자...`를 주세요.")
    if a.env:
        s["env"] = dict(e.split("=", 1) for e in a.env)
    if a.tools:
        s["tools"] = a.tools.split(",")

    def add(data):
        data["servers"] = [x for x in data.get("servers", []) if x["name"] != a.name] + [s]
    open_pr(f"mcp/{stamp()}", f"chore(mcp): {a.name} 추가", edit_json("mcp/mcp.json", {"servers": []}, add))


def cmd_digest(a) -> None:
    s = stats_clone()
    if not s:
        sys.exit("stats 브랜치를 가져오지 못했습니다.")
    since = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
    seen, skill, user, tool = set(), {}, {}, {}
    for f in sorted(s.glob("*.tsv")):
        for i, line in enumerate(f.read_text().splitlines()):
            ts, t, n, sid = (line.split("\t") + ["-"] * 4)[:4]
            if ts[:10] < since:
                continue
            key = (f.stem, sid, n) if sid != "-" else (f.stem, i)
            if key in seen:  # 같은 세션에서 같은 스킬은 한 번만 센다
                continue
            seen.add(key)
            for d, k in ((skill, n), (user, f.stem), (tool, t)):
                d[k] = d.get(k, 0) + 1
    print(f"최근 {a.days}일 팀 스킬 사용: {sum(skill.values())}회 ({since} 이후)")
    for title, d in (("스킬별", skill), ("사람별", user), ("도구별", tool)):
        if d:
            print(f"\n{title}")
            for k, v in sorted(d.items(), key=lambda x: -x[1]):
                print(f"  {k}\t{v}")


def cmd_doctor(a) -> None:
    ok = True

    def check(cond: bool, msg: str, fix: str = "") -> None:
        nonlocal ok
        ok &= cond
        print(("✔ " if cond else "✖ ") + msg + ("" if cond or not fix else f"  → {fix}"))

    check((TEAM / ".git").exists(), "팀 저장소", "aimanager init <팀 저장소 URL>")
    check(bool(config().get("user")), f"사용자: {config().get('user', '-')}", "aimanager init <팀 저장소 URL>")
    behind = git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
    check(behind in ("", "0"), "팀 저장소 최신", "aimanager pull")
    for tool, path in HOOK_FILES.items():
        if shutil.which(tool):
            hooks = load_json(path, {}).get("hooks", {})
            n = sum(str(SCRIPT) in h.get("command", "") for gs in hooks.values() for g in gs for h in g.get("hooks", []))
            check(n == len(HOOK_EVENTS[tool]), f"{tool} 훅 {n}/{len(HOOK_EVENTS[tool])}", "aimanager init <팀 저장소 URL>")
    for target in SKILL_TARGETS:
        missing = [n for n in team_skills() if not (target / n).exists()]
        check(not missing, f"{target} 팀 스킬", f"없음: {', '.join(missing)} (같은 이름의 개인 스킬이 있으면 정상)")
    if shutil.which("codex"):
        print("ℹ Codex 훅은 Codex의 /hooks 에서 신뢰(trust)해야 실행됩니다.")
    sys.exit(0 if ok else 1)


def cmd_uninstall(a) -> None:
    for path in HOOK_FILES.values():
        if path.exists():
            cfg = load_json(path, {})
            strip_our_hooks(cfg.get("hooks", {}))
            save_json(path, cfg)
    for target in SKILL_TARGETS:
        if target.is_dir():
            for link in target.iterdir():
                if link.is_symlink() and is_ours(link):
                    link.unlink()
    for tool, names in load_json(STATE / "managed-mcp.json", {}).items():
        for name in names:
            if shutil.which(tool):
                subprocess.run([tool, "mcp", "remove", *(["-s", "user"] if tool == "claude" else []), name], capture_output=True)
    (STATE / "managed-mcp.json").unlink(missing_ok=True)
    bin_link = HOME / ".local" / "bin" / "aimanager"
    if bin_link.is_symlink():
        bin_link.unlink()
    if a.purge:
        shutil.rmtree(ROOT, ignore_errors=True)
    print("AIManager를 제거했습니다." + ("" if a.purge else f" 데이터는 {ROOT} 에 남아 있습니다 (--purge로 삭제)."))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="aimanager", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="팀 저장소 연결, 훅 등록, 첫 동기화")
    p.add_argument("repo")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("pull", help="팀 스킬·MCP 동기화 (세션 시작 때 자동 실행)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("push", help="스킬을 팀 저장소에 PR로 올리기")
    p.add_argument("skill", help="스킬 이름 또는 폴더 경로")
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser("packages", help="팀 플러그인·npm 도구 설치 / add로 선언 추가")
    p.add_argument("action", nargs="?", choices=["install", "add"], default="install")
    p.add_argument("target", nargs="?")
    p.add_argument("--npm", action="store_true", help="target을 npm 패키지로 취급")
    p.add_argument("--marketplace-repo", help="플러그인 마켓플레이스 저장소 (owner/repo)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_packages)
    p = sub.add_parser("mcp", help="팀 MCP 서버 목록 / 추가·제거 PR")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("name", nargs="?")
    p.add_argument("--url")
    p.add_argument("--transport", choices=["http", "sse"])
    p.add_argument("--header", action="append", help="KEY=VALUE (값에 ${ENV} 사용 가능)")
    p.add_argument("--env", action="append", help="KEY=VALUE")
    p.add_argument("--tools", help="claude,codex 중 일부만")
    p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("digest", help="최근 N일 팀 스킬 사용 요약")
    p.add_argument("days", nargs="?", type=int, default=7)
    p.set_defaults(fn=cmd_digest)
    p = sub.add_parser("doctor", help="설치 상태 점검")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("uninstall", help="이 PC에서 제거")
    p.add_argument("--purge", action="store_true", help="~/.aimanager 까지 삭제")
    p.set_defaults(fn=cmd_uninstall)
    p = sub.add_parser("hook")  # 에이전트 훅 전용
    p.add_argument("event", choices=list(EVENT_ARG.values()))
    p.add_argument("tool", choices=list(HOOK_FILES))
    p.set_defaults(fn=cmd_hook)
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = []
    if argv[:1] == ["mcp"] and "--" in argv:  # stdio 서버 명령: aimanager mcp add 이름 -- npx -y pkg
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.command = tail
    if a.fn is cmd_mcp and a.action != "list" and not a.name:
        ap.error("mcp add/remove에는 이름이 필요합니다.")
    if a.fn is cmd_packages and a.action == "add" and not a.target:
        ap.error("packages add에는 대상이 필요합니다. 예: code-review@claude-plugins-official")
    a.fn(a)


if __name__ == "__main__":
    main()
