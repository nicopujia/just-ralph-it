import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.config import DATA_DIR
from app.database import get_db

from app.prompts.ralphy import RALPHY_SYSTEM_PROMPT
from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["chat"])

ALLOWED_TOOLS = "Bash(git:*) Bash(ls:*) Bash(cat:*) Bash(mv:*) Bash(mkdir:*) Read Glob Grep Write(README.md) Write(.jri/tasks/) Edit(README.md) Edit(.jri/tasks/) WebSearch WebFetch"
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


async def _append_chat_message(project_id: int, msg: dict) -> None:
    """Insert a chat message into the database."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chat_messages (project_id, role, content, thinking_text, thinking_steps) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project_id,
                msg["role"],
                msg.get("content", ""),
                msg.get("thinkingText", ""),
                json.dumps(msg.get("thinkingSteps", [])),
            ),
        )
        await db.commit()


async def _load_chat_messages(project_id: int) -> list[dict]:
    """Load all chat messages for a project from the database."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT role, content, thinking_text, thinking_steps, created_at "
            "FROM chat_messages WHERE project_id = ? ORDER BY id",
            (project_id,),
        )
        rows = await cursor.fetchall()

    messages = []
    for row in rows:
        r = dict(row)
        msg: dict = {"role": r["role"], "content": r["content"]}
        if r["thinking_text"]:
            msg["thinkingText"] = r["thinking_text"]
        steps = json.loads(r["thinking_steps"]) if r["thinking_steps"] else []
        if steps:
            msg["thinkingSteps"] = steps
        messages.append(msg)
    return messages


async def _stream_claude(
    project_name: str,
    project_dir: str,
    project_id: int,
    session_id: str,
    is_new_session: bool,
    user_message: str,
):
    """Async generator that spawns claude CLI and yields SSE events."""
    # Persist user message
    await _append_chat_message(project_id, {"role": "user", "content": user_message})

    # Send an initial keepalive immediately to establish the SSE stream.
    # This prevents proxies (e.g. Cloudflare's 100s initial-response timeout)
    # from dropping the connection before the subprocess starts producing output.
    yield ": keepalive\n\n"

    # If Ralphy is already running for this project, wait for it to finish.
    # Send keepalives while waiting so proxies don't drop the connection.
    existing = _active_procs.get(project_name)
    if existing and existing.returncode is None:
        wait_task = asyncio.ensure_future(existing.wait())
        while not wait_task.done():
            done, _ = await asyncio.wait({wait_task}, timeout=15)
            if not done:
                yield ": keepalive\n\n"

    args = _build_claude_args(session_id, is_new_session, user_message)

    env = {}

    await sse_bus.publish(project_name, "ralphy_processing", {"status": "start"})

    log_dir = Path(project_dir) / ".jri" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ralphy.log"
    log_file = open(log_path, "a", encoding="utf-8")

    # Accumulate assistant response for persistence
    assistant_text = ""
    assistant_thinking = ""
    assistant_tools: list[str] = []

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
                        tool_name = content_block["name"]
                        event = {"type": "tool_use", "name": tool_name, "input": content_block.get("input", {})}
                        yield f"data: {json.dumps(event)}\n\n"
                        log_file.write(f"[tool_use] {tool_name}\n")
                        log_file.flush()
                        assistant_tools.append(tool_name)
                        # Publish issue_update when Ralphy uses Bash (likely task file changes)
                        if tool_name == "Bash":
                            await sse_bus.publish(project_name, "issue_update", {})

                elif msg_type == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_chunk = delta["text"]
                        event = {"type": "text", "content": text_chunk}
                        yield f"data: {json.dumps(event)}\n\n"
                        log_file.write(text_chunk)
                        log_file.flush()
                        assistant_text += text_chunk
                    elif delta.get("type") == "thinking_delta":
                        thinking_chunk = delta["thinking"]
                        event = {"type": "thinking", "content": thinking_chunk}
                        yield f"data: {json.dumps(event)}\n\n"
                        assistant_thinking += thinking_chunk

                elif msg_type == "result":
                    result_text = data.get("result", "")
                    event = {"type": "done", "result": result_text}
                    yield f"data: {json.dumps(event)}\n\n"
                    log_file.write("\n--- Done ---\n")
                    log_file.flush()
                    # Use result text if it's longer (more complete) than streamed text
                    if result_text and len(result_text) >= len(assistant_text):
                        assistant_text = result_text
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
        # Persist assistant response if there was ANY output
        if assistant_text or assistant_thinking or assistant_tools:
            entry: dict = {"role": "assistant", "content": assistant_text}
            if assistant_thinking:
                entry["thinkingText"] = assistant_thinking
            if assistant_tools:
                entry["thinkingSteps"] = assistant_tools
            await _append_chat_message(project_id, entry)
        await sse_bus.publish(project_name, "ralphy_processing", {"status": "end"})
        # Final issue refresh so all clients pick up any task changes Ralphy made
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
            project_id=project["id"],
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


@router.get("/{name}/chat/processing")
async def chat_processing(name: str, user: dict = Depends(get_current_user)):
    """Check if Ralphy is currently processing for this project."""
    await _get_project_for_user(user, name)
    proc = _active_procs.get(name)
    is_processing = proc is not None and proc.returncode is None
    return {"processing": is_processing}


@router.get("/{name}/chat/history")
async def get_chat_history(name: str, user: dict = Depends(get_current_user)):
    project = await _get_project_for_user(user, name)
    messages = await _load_chat_messages(project["id"])
    return {"messages": messages}
