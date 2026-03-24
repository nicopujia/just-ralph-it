import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth_utils import get_current_user
from app.config import DATA_DIR, RALPH_BOT_GITHUB_TOKEN, MAINTENANCE_MODE
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


class CreateProjectRequest(BaseModel):
    name: str
    description: str


async def _run(
    args: list[str], cwd: str | None = None, timeout: float = 30
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError(
            f"Command timed out after {timeout}s: {' '.join(args)}"
        )
    returncode = proc.returncode
    if returncode is None:
        raise RuntimeError(f"Command exited without a return code: {' '.join(args)}")
    return returncode, stdout.decode(), stderr.decode()


async def _get_project_dir(name: str, user: dict) -> str:
    """Verify project belongs to user and return its directory path.

    Raises HTTPException(404) if the project doesn't exist or doesn't
    belong to the authenticated user.
    """
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")

    project_dir = DATA_DIR / github_username / name
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="Project directory not found")

    return str(project_dir)


def _normalize_dependencies(issue: dict) -> list[dict]:
    """Convert bd dependency objects into UI-friendly dependency entries."""
    normalized: list[dict] = []

    for dep in issue.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue

        dep_type = dep.get("type") or "related"
        depends_on_id = dep.get("depends_on_id")
        if not depends_on_id or dep_type == "parent-child":
            continue

        normalized.append({"id": depends_on_id, "type": dep_type})

    return normalized


@router.get("/{name}/issues")
async def list_issues(
    name: str,
    user: dict = Depends(get_current_user),
):
    """List all issues in a project, grouped by parent epic."""
    cwd = await _get_project_dir(name, user)

    rc, stdout, _ = await _run(["bd", "list", "--all", "--json"], cwd=cwd)
    if rc != 0:
        return {"epics": [], "ungrouped": []}

    try:
        issues = json.loads(stdout)
    except json.JSONDecodeError:
        return {"epics": [], "ungrouped": []}

    if not issues:
        return {"epics": [], "ungrouped": []}

    _FIELDS = (
        "id",
        "title",
        "issue_type",
        "status",
        "priority",
        "description",
        "acceptance_criteria",
        "assignee",
        "dependencies",
        "created_at",
    )

    def _pick(issue: dict) -> dict:
        picked = {k: issue.get(k) for k in _FIELDS}
        picked["dependencies"] = _normalize_dependencies(issue)
        return picked

    epics_map: dict[str, dict] = {}  # epic id -> epic dict with children
    ungrouped: list[dict] = []

    # First pass: identify epics (type == "epic" or has children via dotted ids)
    for issue in issues:
        iid = issue.get("id", "")
        if issue.get("issue_type") == "epic" or (
            "." not in iid
            and any(
                other.get("id", "").startswith(iid + ".")
                for other in issues
                if other.get("id", "") != iid
            )
        ):
            epics_map[iid] = {
                "id": iid,
                "title": issue.get("title", ""),
                "status": issue.get("status", ""),
                "children": [],
            }

    # Second pass: assign children to epics
    for issue in issues:
        iid = issue.get("id", "")
        if iid in epics_map:
            continue

        parent = issue.get("parent", "")
        if not parent and "." in iid:
            parent = iid.rsplit(".", 1)[0]

        if parent and parent in epics_map:
            epics_map[parent]["children"].append(_pick(issue))
        else:
            ungrouped.append(_pick(issue))

    return {
        "epics": list(epics_map.values()),
        "ungrouped": ungrouped,
    }


@router.get("/{name}/issues/{issue_id}")
async def get_issue(
    name: str,
    issue_id: str,
    user: dict = Depends(get_current_user),
):
    """Return full details for a single issue."""
    cwd = await _get_project_dir(name, user)

    rc, stdout, stderr = await _run(["bd", "show", issue_id, "--json"], cwd=cwd)
    if rc != 0:
        raise HTTPException(
            status_code=404, detail=f"Issue not found: {stderr.strip()}"
        )

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse issue data")


@router.post("")
async def create_project(
    body: CreateProjectRequest,
    user: dict = Depends(get_current_user),
):
    name = body.name
    # Maintenance mode: save interested user's email and reject
    if MAINTENANCE_MODE:
        email = user.get("github_email") or f"{user['github_username']}@users.noreply.github.com"
        waitlist_path = DATA_DIR / "waitlist.txt"
        # Append if not already in the list
        existing = waitlist_path.read_text() if waitlist_path.exists() else ""
        if email not in existing:
            with open(waitlist_path, "a") as f:
                f.write(f"{email}\n")
        raise HTTPException(
            status_code=503,
            detail="We're currently in maintenance. We've saved your email and will notify you when we're back!",
        )
    description = body.description

    # --- Token check ---
    if not RALPH_BOT_GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GitHub bot token not configured")

    # --- Validation ---
    if not (1 <= len(name) <= 100) or not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid project name")

    github_username: str = user["github_username"]
    user_name: str = user.get("github_name") or github_username
    user_email: str = (
        user.get("github_email")
        or f"{github_username}@users.noreply.github.com"
    )
    user_id: int = user["id"]

    # Check uniqueness in DB
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if await cursor.fetchone():
            raise HTTPException(status_code=409, detail="Project name already exists")

    project_dir = DATA_DIR / github_username / name
    github_repo_url = f"https://github.com/ralphpujia/{github_username}-{name}"
    token = RALPH_BOT_GITHUB_TOKEN

    try:
        # 1. Create project directory
        logger.info(f"Creating project {name}: step 1 - creating project directory")
        project_dir.mkdir(parents=True, exist_ok=True)
        cwd = str(project_dir)

        # 2. git init
        logger.info(f"Creating project {name}: step 2 - git init")
        rc, _, err = await _run(["git", "init", "-b", "main"], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"git init failed: {err}")

        # 3. git config
        logger.info(f"Creating project {name}: step 3 - git config")
        await _run(["git", "config", "user.name", "ralphpujia"], cwd=cwd)
        await _run(
            ["git", "config", "user.email", "ralphpujia@users.noreply.github.com"],
            cwd=cwd,
        )

        # 4. bd init (with retry — shared Dolt server may need a moment)
        logger.info(f"Creating project {name}: step 4 - bd init")
        # Ensure shared Dolt server is running before attempting bd init
        shared_pid_file = Path.home() / ".beads" / "shared-server" / "dolt-server.pid"
        server_alive = False
        if shared_pid_file.exists():
            try:
                pid = int(shared_pid_file.read_text().strip())
                os.kill(pid, 0)
                server_alive = True
            except (ValueError, OSError):
                pass
        if not server_alive:
            await _run(["bd", "dolt", "start"], cwd=str(Path.home()), timeout=30)
        last_bd_init_error = ""
        for attempt in range(3):
            try:
                rc, out, err = await _run(
                    [
                        "bd",
                        "init",
                        "--shared-server",
                        "-p",
                        name,
                        "--agents-template", "/dev/null",
                    ],
                    cwd=cwd,
                    timeout=10,
                )
            except RuntimeError as exc:
                rc = -1
                out = ""
                err = str(exc)

            last_bd_init_error = err or out or f"bd init failed with rc={rc}"
            if rc == 0:
                break
            logger.warning(
                "bd init attempt %d failed (rc=%d): %s",
                attempt + 1,
                rc,
                last_bd_init_error,
            )
            if attempt < 2:
                await asyncio.sleep(2)
        if rc != 0:
            raise RuntimeError(
                f"bd init failed after 3 attempts: {last_bd_init_error}"
            )

        # 4b. Install beads git hooks (auto-commit issues to repo)
        logger.info(f"Creating project {name}: step 4b - bd hooks install")
        rc, _, err = await _run(["bd", "hooks", "install"], cwd=cwd)
        if rc != 0:
            logger.warning("bd hooks install failed (rc=%d): %s", rc, err)

        # 5. Create CLAUDE.md
        logger.info(f"Creating project {name}: step 5 - creating CLAUDE.md")
        agents_md = (
            f"# {name}\n"
            f"\n"
            f"{description}\n"
            f"\n"
            f"## Project Info\n"
            f"- Repository: {github_repo_url}\n"
            f"- Created by: {github_username}"
        )
        (project_dir / "CLAUDE.md").write_text(agents_md)

        # 6. Create uploads/ directory
        logger.info(f"Creating project {name}: step 6 - creating uploads directory")
        (project_dir / "uploads").mkdir(exist_ok=True)

        # 6b. Create .env file and .gitignore
        (project_dir / ".env").write_text("")
        (project_dir / ".gitignore").write_text(".env\n")

        # 7. Git add all and commit
        logger.info(f"Creating project {name}: step 7 - git add and commit")
        await _run(["git", "add", "."], cwd=cwd)
        commit_msg = (
            f"Initial project setup\n"
            f"\n"
            f"Co-authored-by: {user_name} <{user_email}>"
        )
        rc, _, err = await _run(["git", "commit", "-m", commit_msg], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"git commit failed: {err}")

        # 8. Create GitHub repo (with retry on 422 "already exists")
        logger.info(f"Creating project {name}: step 8 - creating GitHub repo")
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as client:
            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                resp = await client.post(
                    "https://api.github.com/user/repos",
                    headers=headers,
                    json={
                        "name": f"{github_username}-{name}",
                        "description": description[:350] if description else "",
                        "private": True,
                        "auto_init": False,
                    },
                    timeout=30,
                )
                if resp.status_code == 422 and "already exists" in resp.text.lower():
                    if attempt < max_attempts:
                        logger.info(
                            f"Creating project {name}: GitHub repo already exists, "
                            f"retrying in 2s (attempt {attempt}/{max_attempts})"
                        )
                        await asyncio.sleep(2)
                        continue
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A GitHub repo with this name already exists or was recently deleted. "
                            "Please wait a moment and try again, or choose a different name."
                        ),
                    )
                if resp.status_code == 422:
                    raise RuntimeError(
                        f"GitHub repo validation error ({resp.status_code}): {resp.text}"
                    )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"GitHub create repo failed ({resp.status_code}): {resp.text}"
                    )
                break  # success

            # 9. Add user as collaborator
            logger.info(f"Creating project {name}: step 9 - adding collaborator {github_username}")
            resp2 = await client.put(
                f"https://api.github.com/repos/ralphpujia/{github_username}-{name}/collaborators/{github_username}",
                headers=headers,
                json={"permission": "push"},
                timeout=30,
            )
            if resp2.status_code >= 400:
                raise RuntimeError(
                    f"GitHub add collaborator failed ({resp2.status_code}): {resp2.text}"
                )

        # 10. Add remote
        logger.info(f"Creating project {name}: step 10 - adding git remote")
        rc, _, err = await _run(
            [
                "git",
                "remote",
                "add",
                "origin",
                f"https://x-access-token:{token}@github.com/ralphpujia/{github_username}-{name}.git",
            ],
            cwd=cwd,
        )
        if rc != 0:
            raise RuntimeError(f"git remote add failed: {err}")

        # 11. Push
        logger.info(f"Creating project {name}: step 11 - git push")
        rc, _, err = await _run(["git", "push", "-u", "origin", "main"], cwd=cwd)
        if rc != 0:
            raise RuntimeError(f"git push failed: {err}")

    except HTTPException:
        # Re-raise HTTP exceptions (like 409) after cleanup
        if project_dir.exists():
            shutil.rmtree(project_dir)
        raise
    except Exception as exc:
        logger.exception(f"Failed to create project {name}")
        # Clean up on any failure
        if project_dir.exists():
            shutil.rmtree(project_dir)
        raise HTTPException(status_code=500, detail=str(exc))

    # 12. Insert into SQLite
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO projects (user_id, name, description, github_repo_url) VALUES (?, ?, ?, ?)",
            (user_id, name, description, github_repo_url),
        )
        project_id = cursor.lastrowid
        deploy_port = 9000 + project_id
        deploy_subdomain = name.lower()
        await db.execute(
            "UPDATE projects SET deploy_port = ?, deploy_subdomain = ? WHERE id = ?",
            (deploy_port, deploy_subdomain, project_id),
        )
        await db.commit()

    # 13. Return JSON
    return {
        "id": project_id,
        "name": name,
        "description": description,
        "github_repo_url": github_repo_url,
        "deploy_port": deploy_port,
        "deploy_subdomain": deploy_subdomain,
    }


async def _get_issue_count(project_dir: str) -> int:
    """Run `bd list --json` in the project directory and count issues."""
    try:
        rc, stdout, _ = await _run(["bd", "list", "--json"], cwd=project_dir)
        if rc != 0:
            return 0
        issues = json.loads(stdout)
        return len(issues)
    except Exception:
        return 0


@router.get("")
async def list_projects(user: dict = Depends(get_current_user)):
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, description, github_repo_url, ralph_loop_status, created_at "
            "FROM projects WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()

    row_dicts = [dict(row) for row in rows]
    project_dirs = [str(DATA_DIR / github_username / r["name"]) for r in row_dicts]
    issue_counts = await asyncio.gather(*[_get_issue_count(d) for d in project_dirs])

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "github_repo_url": r["github_repo_url"],
            "issue_count": count,
            "ralph_loop_status": r["ralph_loop_status"],
            "created_at": r["created_at"],
        }
        for r, count in zip(row_dicts, issue_counts)
    ]


@router.get("/{name}/agents-md")
async def get_agents_md(name: str, user: dict = Depends(get_current_user)):
    github_username: str = user["github_username"]
    user_id: int = user["id"]

    # Verify project belongs to user
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")

    agents_path = DATA_DIR / github_username / name / "CLAUDE.md"

    if not agents_path.exists():
        return {"content": "", "exists": False}

    content = agents_path.read_text()
    return {"content": content, "exists": True}


@router.get("/{name}/env")
async def get_env(name: str, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    github_username = user["github_username"]
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
    env_path = DATA_DIR / github_username / name / ".env"
    content = env_path.read_text() if env_path.exists() else ""
    return {"content": content}


class EnvUpdateRequest(BaseModel):
    content: str


@router.put("/{name}/env")
async def update_env(name: str, body: EnvUpdateRequest, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    github_username = user["github_username"]
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
    env_path = DATA_DIR / github_username / name / ".env"
    env_path.write_text(body.content)
    return {"status": "saved"}


@router.get("/{name}")
async def get_project(name: str, user: dict = Depends(get_current_user)):
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, description, github_repo_url, ralph_session_id, "
            "ralph_loop_status, ralph_loop_current_issue, ralph_loop_iteration, "
            "deploy_type, deploy_port, deploy_status, deploy_subdomain, created_at "
            "FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    row_dict = dict(row)
    project_dir = str(DATA_DIR / github_username / row_dict["name"])
    issue_count = await _get_issue_count(project_dir)

    return {
        "id": row_dict["id"],
        "name": row_dict["name"],
        "description": row_dict["description"],
        "github_repo_url": row_dict["github_repo_url"],
        "issue_count": issue_count,
        "ralph_session_id": row_dict["ralph_session_id"],
        "ralph_loop_status": row_dict["ralph_loop_status"],
        "ralph_loop_current_issue": row_dict["ralph_loop_current_issue"],
        "ralph_loop_iteration": row_dict["ralph_loop_iteration"],
        "deploy_type": row_dict["deploy_type"],
        "deploy_port": row_dict["deploy_port"],
        "deploy_status": row_dict["deploy_status"],
        "deploy_subdomain": row_dict["deploy_subdomain"],
        "created_at": row_dict["created_at"],
    }


class DeployRequest(BaseModel):
    type: str  # 'static' or 'dynamic'
    start_command: str | None = None


@router.post("/{name}/deploy")
async def deploy_project(
    name: str,
    body: DeployRequest,
    user: dict = Depends(get_current_user),
):
    """Configure deployment for a project."""
    if body.type not in ("static", "dynamic"):
        raise HTTPException(status_code=400, detail="type must be 'static' or 'dynamic'")

    user_id: int = user["id"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, deploy_port, deploy_subdomain FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")

        row_dict = dict(row)
        project_id = row_dict["id"]
        deploy_port = row_dict["deploy_port"] or (9000 + project_id)
        deploy_subdomain = row_dict["deploy_subdomain"] or name.lower()

        await db.execute(
            "UPDATE projects SET deploy_type = ?, deploy_start_command = ?, "
            "deploy_status = 'running', deploy_port = ?, deploy_subdomain = ? WHERE id = ?",
            (body.type, body.start_command, deploy_port, deploy_subdomain, project_id),
        )
        await db.commit()

    return {
        "subdomain_url": f"https://{deploy_subdomain}.justralph.it",
        "port": deploy_port,
    }


@router.post("/{name}/deploy/stop")
async def stop_deploy(
    name: str,
    user: dict = Depends(get_current_user),
):
    """Stop deployment for a project."""
    user_id: int = user["id"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")

        await db.execute(
            "UPDATE projects SET deploy_status = 'stopped' WHERE id = ?",
            (dict(row)["id"],),
        )
        await db.commit()

    return {"deploy_status": "stopped"}


@router.delete("/{name}", status_code=204)
async def delete_project(
    name: str,
    delete_repo: bool = True,
    user: dict = Depends(get_current_user),
):
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, ralph_loop_status FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    row_dict = dict(row)

    if row_dict["ralph_loop_status"] == "running":
        raise HTTPException(
            status_code=409, detail="Cannot delete while Ralph is running"
        )

    # Delete GitHub repo if requested
    if delete_repo:
        token = RALPH_BOT_GITHUB_TOKEN
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"https://api.github.com/repos/ralphpujia/{github_username}-{name}",
                headers=headers,
                timeout=30,
            )
            # Ignore 404 (repo already gone)
            if resp.status_code >= 400 and resp.status_code != 404:
                raise HTTPException(
                    status_code=500,
                    detail=f"GitHub delete repo failed ({resp.status_code}): {resp.text}",
                )

    # Delete project directory
    project_dir = DATA_DIR / github_username / name
    if project_dir.exists():
        shutil.rmtree(project_dir)

    # Delete from SQLite
    async with get_db() as db:
        await db.execute("DELETE FROM projects WHERE id = ?", (row_dict["id"],))
        await db.commit()

    return Response(status_code=204)
