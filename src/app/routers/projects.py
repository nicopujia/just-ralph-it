import asyncio
import logging
import re
import shutil

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth_utils import get_current_user
from app.config import DATA_DIR, RALPH_BOT_GITHUB_TOKEN
from app.database import get_db
from app import tasks
from app.whitelist import check_whitelist

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


@router.get("/{name}/tasks")
async def list_tasks(
    name: str,
    user: dict = Depends(get_current_user),
):
    """List all tasks in a project."""
    cwd = await _get_project_dir(name, user)
    return tasks.list_all(cwd)


@router.get("/{name}/tasks/{task_id}")
async def get_task(
    name: str,
    task_id: str,
    user: dict = Depends(get_current_user),
):
    """Return full details for a single task."""
    cwd = await _get_project_dir(name, user)
    task = tasks.get_task(cwd, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("")
async def create_project(
    body: CreateProjectRequest,
    user: dict = Depends(get_current_user),
):
    name = body.name
    # Closed beta: only whitelisted users can create projects
    check_whitelist(user)
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

        # 4. Initialize task tracking
        logger.info(f"Creating project {name}: step 4 - init tasks")
        tasks.init_tasks(cwd)

        # 5. Create README.md
        logger.info(f"Creating project {name}: step 5 - creating README.md")
        readme_md = (
            f"# {name}\n"
            f"\n"
            f"{description}\n"
            f"\n"
            f"## Project Info\n"
            f"- Repository: {github_repo_url}\n"
            f"- Created by: {github_username}"
        )
        (project_dir / "README.md").write_text(readme_md)

        # 6. Create .jri/uploads/ and .jri/logs/ directories
        logger.info(f"Creating project {name}: step 6 - creating .jri subdirectories")
        (project_dir / ".jri" / "uploads").mkdir(parents=True, exist_ok=True)
        (project_dir / ".jri" / "logs").mkdir(parents=True, exist_ok=True)

        # 6a. Write .jri/README.md
        jri_readme = (
            "# .jri/ — Just Ralph It\n"
            "\n"
            "Project tracking directory managed by [JRI](https://justralph.it).\n"
            "\n"
            "## Structure\n"
            "\n"
            "```\n"
            "tasks/      — Markdown task files with YAML frontmatter, organized by status\n"
            "  draft/    — tasks being defined\n"
            "  todo/     — tasks ready to start\n"
            "  doing/    — tasks in progress\n"
            "  done/     — completed tasks\n"
            "uploads/    — user-uploaded reference files\n"
            "logs/       — agent output logs\n"
            "  ralph.log   — Ralph (builder) output log\n"
            "  ralphy.log  — Ralphy (interviewer) output log\n"
            "```\n"
            "\n"
            "Each task is a Markdown file with YAML frontmatter, named by its slug (e.g. `implement-dashboard-page.md`).\n"
            "Status is determined by which subdirectory the file lives in.\n"
        )
        (project_dir / ".jri" / "README.md").write_text(jri_readme)

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
        deploy_subdomain = f"{name.lower()}.{github_username.lower()}"
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


def _get_issue_count(project_dir: str) -> int:
    try:
        return tasks.task_count(project_dir)
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
    issue_counts = [_get_issue_count(d) for d in project_dirs]

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


@router.get("/{name}/readme")
async def get_readme(name: str, user: dict = Depends(get_current_user)):
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

    readme_path = DATA_DIR / github_username / name / "README.md"

    if not readme_path.exists():
        return {"content": "", "exists": False}

    content = readme_path.read_text()
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
    issue_count = _get_issue_count(project_dir)

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
    start_command: str | None = None


@router.post("/{name}/deploy")
async def deploy_project(
    name: str,
    body: DeployRequest,
    user: dict = Depends(get_current_user),
):
    """Configure deployment for a project."""

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
        deploy_subdomain = row_dict["deploy_subdomain"] or f"{name.lower()}.{user['github_username'].lower()}"

        await db.execute(
            "UPDATE projects SET deploy_type = 'dynamic', deploy_start_command = ?, "
            "deploy_status = 'running', deploy_port = ?, deploy_subdomain = ? WHERE id = ?",
            (body.start_command, deploy_port, deploy_subdomain, project_id),
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
