import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.config import DATA_DIR
from app.database import get_db

from app.prompts.ralphy import RALPHY_SYSTEM_PROMPT
from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["chat"])

ALLOWED_TOOLS = "Bash(bd:*) Bash(git:*) Read Glob Grep Write(README.md) Edit(README.md) WebSearch WebFetch"
MAX_MESSAGE_LENGTH = 50_000

ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
MAX_FILE_SIZE = 3 * 1024 * 1024  # 3 MB
MAX_ATTACHMENTS = 3


class ChatRequest(BaseModel):
    message: str


async def _get_project_for_user(user: dict, project_name: str) -> dict:
    """Fetch a project row ensuring it belongs to the authenticated user."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM projects WHERE user_id = ? AND name = ?",
            (user["id"], project_name),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


async def _ensure_session_id(project_id: int, current_session_id: str | None) -> tuple[str, bool]:
    """Return (session_id, is_new). Creates and stores a new UUID if needed."""
    if current_session_id:
        return current_session_id, False

    session_id = str(uuid.uuid4())
    async with get_db() as db:
        await db.execute(
            "UPDATE projects SET ralph_session_id = ? WHERE id = ?",
            (session_id, project_id),
        )
        await db.commit()
    return session_id, True


def _build_claude_args(
    session_id: str, is_new_session: bool, user_message: str
) -> list[str]:
    """Build the argument list for the claude CLI subprocess."""
    args = ["claude", "-p", "--model", "opus"]

    if is_new_session:
        args += ["--session-id", session_id]
    else:
        args += ["--resume", session_id, "--continue"]

    args += [
        "--system-prompt", RALPHY_SYSTEM_PROMPT,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", ALLOWED_TOOLS,
        "--", user_message,
    ]
    return args


async def _validate_attachments(attachments: list[UploadFile]) -> list[tuple[str, bytes]]:
    """Validate attachments and return list of (filename, content) tuples."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many attachments. Maximum is {MAX_ATTACHMENTS}.",
        )

    validated: list[tuple[str, bytes]] = []
    for attachment in attachments:
        if attachment.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"File type '{attachment.content_type}' is not allowed. "
                       f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}",
            )

        content = await attachment.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{attachment.filename}' exceeds the 3MB size limit.",
            )

        validated.append((attachment.filename or "unnamed", content))

    return validated


def _save_attachments_to_uploads(
    project_dir: str, validated: list[tuple[str, bytes]]
) -> list[str]:
    """Save validated attachments to .jri/uploads/ and return list of filenames."""
    uploads_dir = Path(project_dir) / ".jri" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    filenames: list[str] = []
    for filename, content in validated:
        dest = uploads_dir / filename
        dest.write_bytes(content)
        filenames.append(filename)

    return filenames


def _prepend_attachment_info(message: str, filenames: list[str]) -> str:
    """Prepend attachment names to the user message."""
    names = ", ".join(filenames)
    return f"Attachments: {names}\n\n{message}"


_active_procs: dict[str, asyncio.subprocess.Process] = {}


async def _stream_claude(
    project_name: str,
    project_dir: str,
    session_id: str,
    is_new_session: bool,
    user_message: str,
):
    """Async generator that spawns claude CLI and yields SSE events."""
    # If Ralphy is already running for this project, wait for it to finish
    existing = _active_procs.get(project_name)
    if existing and existing.returncode is None:
        await existing.wait()

    args = _build_claude_args(session_id, is_new_session, user_message)

    env = {"BD_ACTOR": "ralphy"}

    await sse_bus.publish(project_name, "ralphy_processing", {"status": "start"})

    log_dir = Path(project_dir) / ".jri"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ralphy.log"
    log_file = open(log_path, "a", encoding="utf-8")

    try:
        got_result = False
        max_attempts = 2

        for attempt in range(max_attempts):
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **env},
            )
            _active_procs[project_name] = proc

            while True:
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
                except (asyncio.TimeoutError, TimeoutError):
                    # Keep connection alive during long tool executions
                    yield ": keepalive\n\n"
                    continue

                if not raw_line:
                    break  # EOF

                line = raw_line.decode().strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                if msg_type == "content_block_start":
                    content_block = data.get("content_block", {})
                    if content_block.get("type") == "tool_use":
                        event = {"type": "tool_use", "name": content_block["name"], "input": content_block.get("input", {})}
                        yield f"data: {json.dumps(event)}\n\n"
                        log_file.write(f"[tool_use] {content_block['name']}\n")
                        log_file.flush()
                        # Publish issue_update when Ralphy uses Bash (likely bd commands)
                        if content_block["name"] == "Bash":
                            await sse_bus.publish(project_name, "issue_update", {})

                elif msg_type == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        event = {"type": "text", "content": delta["text"]}
                        yield f"data: {json.dumps(event)}\n\n"
                        log_file.write(delta["text"])
                        log_file.flush()
                    elif delta.get("type") == "thinking_delta":
                        event = {"type": "thinking", "content": delta["thinking"]}
                        yield f"data: {json.dumps(event)}\n\n"

                elif msg_type == "result":
                    result_text = data.get("result", "")
                    event = {"type": "done", "result": result_text}
                    yield f"data: {json.dumps(event)}\n\n"
                    log_file.write("\n--- Done ---\n")
                    log_file.flush()
                    got_result = True

            await proc.wait()

            if got_result or proc.returncode == 0:
                break

            # Non-zero exit with no result — retry once
            if attempt < max_attempts - 1:
                stderr_bytes = await proc.stderr.read()
                last_stderr = stderr_bytes.decode().strip()
                continue

            # Final attempt failed
            stderr_bytes = await proc.stderr.read()
            stderr_text = stderr_bytes.decode().strip()
            event = {
                "type": "error",
                "message": f"Claude exited with code {proc.returncode}: {stderr_text}",
            }
            yield f"data: {json.dumps(event)}\n\n"

    except Exception as exc:
        event = {"type": "error", "message": str(exc)}
        yield f"data: {json.dumps(event)}\n\n"

    finally:
        log_file.close()
        _active_procs.pop(project_name, None)
        await sse_bus.publish(project_name, "ralphy_processing", {"status": "end"})
        # Final issue refresh so all clients pick up any bd changes Ralphy made
        await sse_bus.publish(project_name, "issue_update", {})


@router.post("/{name}/chat")
async def chat(
    name: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    project = await _get_project_for_user(user, name)
    github_username: str = user["github_username"]
    project_dir = str(DATA_DIR / github_username / name)

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        message = form.get("message")
        if not message or not isinstance(message, str):
            raise HTTPException(status_code=400, detail="Field 'message' is required.")

        attachments: list[UploadFile] = [
            v for _, v in form.multi_items()
            if isinstance(v, UploadFile)
        ]

        if attachments:
            validated = await _validate_attachments(attachments)
            filenames = _save_attachments_to_uploads(project_dir, validated)
            message = _prepend_attachment_info(message, filenames)

        user_message = message
    else:
        # Assume JSON
        try:
            body = ChatRequest(**(await request.json()))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        user_message = body.message

    if len(user_message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Message too long. Maximum {MAX_MESSAGE_LENGTH} characters.",
        )

    session_id, is_new = await _ensure_session_id(
        project["id"], project.get("ralph_session_id")
    )

    return StreamingResponse(
        _stream_claude(
            project_name=name,
            project_dir=project_dir,
            session_id=session_id,
            is_new_session=is_new,
            user_message=user_message,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _extract_text_content(content) -> str:
    """Extract text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


@router.get("/{name}/chat/history")
async def get_chat_history(name: str, user: dict = Depends(get_current_user)):
    project = await _get_project_for_user(user, name)
    session_id = project.get("ralph_session_id")

    if not session_id:
        return {"messages": []}

    github_username: str = user["github_username"]
    session_file = (
        Path.home()
        / ".claude"
        / "projects"
        / f"-home-nico-jri-data-{github_username}-{name}"
        / f"{session_id}.jsonl"
    )

    if not session_file.exists():
        return {"messages": []}

    messages = []
    for line in session_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = None
        if msg.get("type") == "user" and msg.get("message", {}).get("role") == "user":
            role = "user"
            content = _extract_text_content(msg.get("message", {}).get("content", ""))
        elif msg.get("type") == "assistant" and msg.get("message", {}).get("role") == "assistant":
            role = "assistant"
            content = _extract_text_content(msg.get("message", {}).get("content", ""))
        else:
            continue

        if content:
            messages.append({"role": role, "content": content})

    # Merge consecutive assistant messages (tool-use fragments during thinking)
    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == "assistant" and msg["role"] == "assistant":
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append(msg)

    return {"messages": merged}
