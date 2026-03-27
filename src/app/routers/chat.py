import asyncio
import json
import os
import socket
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.config import DATA_DIR, RALPHY_MODEL
from app.database import get_db
from app.prompts.ralphy import RALPHY_SYSTEM_PROMPT
from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["chat"])

ALLOWED_TOOLS = (
    "Bash(git:*) Bash(ls:*) Bash(cat:*) Bash(mv:*) Bash(mkdir:*)"
    " Read Glob Grep Write(README.md) Write(.jri/tasks/)"
    " Edit(README.md) Edit(.jri/tasks/) WebSearch WebFetch"
)
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


async def _ensure_session_id(
    project_id: int, current_session_id: str | None
) -> tuple[str, bool]:
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


async def _validate_attachments(
    attachments: list[UploadFile],
) -> list[tuple[str, bytes]]:
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


# OpenCode server management
_opencode_servers: dict[
    str, dict
] = {}  # project_name -> {"process": Process, "port": int, "client": httpx.AsyncClient}
_active_sessions: dict[str, str] = {}  # project_name -> opencode_session_id


def _find_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _ensure_opencode_server(
    project_name: str, project_dir: str
) -> httpx.AsyncClient:
    """Start an OpenCode server for the project if needed, return client."""
    server = _opencode_servers.get(project_name)
    if server:
        # Check if process is still alive
        if server["process"].returncode is None:
            return server["client"]
        # Process died, clean up
        await server["client"].aclose()
        _opencode_servers.pop(project_name, None)

    port = _find_free_port()

    config = {
        "model": RALPHY_MODEL,
        "permission": {"*": "allow"},
        "compaction": {
            "auto": True,
            "prune": True,
            "reserved": 30000,  # Higher = earlier compaction (leave more buffer)
        },
        "agent": {
            "ralphy": {
                "mode": "primary",
                "description": "AI planning assistant for task creation",
                "prompt": RALPHY_SYSTEM_PROMPT,
                "tools": {
                    "bash": True,
                    "edit": True,
                    "write": True,
                    "read": True,
                    "grep": True,
                    "glob": True,
                    "webfetch": True,
                    "websearch": True,
                },
            }
        },
        "default_agent": "ralphy",
    }

    # Isolate opencode config/session to project dir (avoids global config conflicts)
    jri_opencode_dir = str(Path(project_dir) / ".jri" / "opencode")
    Path(jri_opencode_dir).mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "OPENCODE_CONFIG_CONTENT": json.dumps(config),
        "OPENCODE_CONFIG_DIR": jri_opencode_dir,
        "OPENCODE_SESSION_DIR": jri_opencode_dir,
    }
    env.pop("OPENCODE_CONFIG", None)

    proc = await asyncio.create_subprocess_exec(
        "/home/linuxbrew/.linuxbrew/bin/opencode",
        "serve",
        "--port",
        str(port),
        "--hostname",
        "127.0.0.1",
        cwd=project_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    base_url = f"http://127.0.0.1:{port}"
    client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(None))

    # Wait for health check
    deadline = asyncio.get_event_loop().time() + 60
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get("/global/health")
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadError, OSError):
            pass
        # Check if process died
        if proc.returncode is not None:
            await client.aclose()
            stderr = await proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(
                f"opencode serve exited with code {proc.returncode}: {stderr.decode()}"
            )
        await asyncio.sleep(0.5)
    else:
        proc.terminate()
        await client.aclose()
        raise RuntimeError("opencode serve did not become healthy within 60s")

    _opencode_servers[project_name] = {
        "process": proc,
        "port": port,
        "client": client,
    }
    return client


async def _append_chat_message(project_id: int, msg: dict) -> None:
    """Insert a chat message into the database."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chat_messages"
            " (project_id, role, content,"
            " thinking_text, thinking_steps)"
            " VALUES (?, ?, ?, ?, ?)",
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


async def _stream_opencode(
    project_name: str,
    project_dir: str,
    project_id: int,
    session_id: str,
    is_new_session: bool,
    user_message: str,
):
    """Async generator that uses OpenCode HTTP API and yields SSE events."""
    # Persist user message
    await _append_chat_message(project_id, {"role": "user", "content": user_message})

    # Initial keepalive to establish SSE stream
    yield ": keepalive\n\n"

    await sse_bus.publish(project_name, "ralphy_processing", {"status": "start"})

    # Accumulate assistant response for persistence
    assistant_text = ""
    assistant_thinking = ""
    assistant_tools: list[str] = []
    saved_to_db = False
    log_file = None

    try:
        # Setup log file - may raise OSError/PermissionError
        log_dir = Path(project_dir) / ".jri" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "ralphy.log"
        log_file = open(log_path, "a", encoding="utf-8")
        # Get or start OpenCode server
        client = await _ensure_opencode_server(project_name, project_dir)

        # Get or create OpenCode session
        oc_session_id = _active_sessions.get(project_name)
        if not oc_session_id:
            resp = await client.post("/session")
            resp.raise_for_status()
            session_data = resp.json()
            oc_session_id = session_data.get("sessionID") or session_data.get("id")
            _active_sessions[project_name] = oc_session_id

        # Send prompt async
        resp = await client.post(
            f"/session/{oc_session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": user_message}]},
        )
        resp.raise_for_status()

        # Stream events from /event SSE endpoint
        last_event_time = asyncio.get_event_loop().time()

        async with client.stream("GET", "/event") as stream:
            buffer = ""
            async for chunk in stream.aiter_text():
                buffer += chunk
                # Process complete SSE messages (separated by double newlines)
                while "\n\n" in buffer:
                    message_str, buffer = buffer.split("\n\n", 1)
                    last_event_time = asyncio.get_event_loop().time()

                    # Parse SSE data lines
                    data_lines = []
                    for sse_line in message_str.split("\n"):
                        if sse_line.startswith("data: "):
                            data_lines.append(sse_line[6:])
                        elif sse_line.startswith("data:"):
                            data_lines.append(sse_line[5:])
                    if not data_lines:
                        continue

                    try:
                        data = json.loads("".join(data_lines))
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type", "")
                    props = data.get("properties", {})

                    # Filter by session
                    evt_session = props.get("sessionID", "")
                    if evt_session and evt_session != oc_session_id:
                        continue

                    # Filter out session title/summary parts
                    part = props.get("part", {})
                    if isinstance(part, dict):
                        part_role = part.get("role", "")
                        if part_role in ("system", "summary", "metadata"):
                            continue

                    # Map OpenCode events to frontend SSE format
                    if event_type == "message.part.updated":
                        part_type = part.get("type", "")

                        if part_type == "text":
                            text_content = part.get("text", "")
                            if text_content:
                                event = {"type": "text", "content": text_content}
                                yield f"data: {json.dumps(event)}\n\n"
                                log_file.write(text_content)
                                log_file.flush()
                                assistant_text += text_content

                        elif part_type == "reasoning":
                            thinking_content = part.get("text", "")
                            if thinking_content:
                                event = {
                                    "type": "thinking",
                                    "content": thinking_content,
                                }
                                yield f"data: {json.dumps(event)}\n\n"
                                assistant_thinking += thinking_content

                        elif part_type == "tool_use":
                            tool_name = part.get("name", "unknown")
                            tool_input = part.get("input", part.get("args", {}))
                            event = {
                                "type": "tool_use",
                                "name": tool_name,
                                "input": tool_input,
                            }
                            yield f"data: {json.dumps(event)}\n\n"
                            log_file.write(f"[tool_use] {tool_name}\n")
                            log_file.flush()
                            if tool_name not in assistant_tools:
                                assistant_tools.append(tool_name)
                            if tool_name.lower() in ("bash", "write", "edit"):
                                await sse_bus.publish(project_name, "issue_update", {})

                    elif event_type == "session.idle":
                        # Persist assistant message BEFORE sending done event
                        # so it's in the DB when the client fetches history
                        if assistant_text.strip():
                            entry: dict = {
                                "role": "assistant",
                                "content": assistant_text,
                            }
                            if assistant_thinking:
                                entry["thinkingText"] = assistant_thinking
                            if assistant_tools:
                                entry["thinkingSteps"] = assistant_tools
                            await _append_chat_message(project_id, entry)
                            saved_to_db = True

                        event = {"type": "done", "result": assistant_text}
                        yield f"data: {json.dumps(event)}\n\n"
                        log_file.write("\n--- Done ---\n")
                        log_file.flush()
                        break

                    elif event_type == "session.error":
                        error_data = props.get("error", {})
                        error_msg = error_data.get("data", {}).get("message") or str(
                            error_data
                        )
                        event = {"type": "error", "message": error_msg}
                        yield f"data: {json.dumps(event)}\n\n"
                        break

                # Send keepalive if no events for 15s
                now = asyncio.get_event_loop().time()
                if now - last_event_time > 15:
                    yield ": keepalive\n\n"
                    last_event_time = now

    except Exception as exc:
        event = {"type": "error", "message": str(exc)}
        yield f"data: {json.dumps(event)}\n\n"

    finally:
        if log_file:
            log_file.close()
        # Only save in finally if not already saved (error/partial response case)
        if not saved_to_db and assistant_text.strip():
            entry_final: dict = {
                "role": "assistant",
                "content": assistant_text,
            }
            if assistant_thinking:
                entry_final["thinkingText"] = assistant_thinking
            if assistant_tools:
                entry_final["thinkingSteps"] = assistant_tools
            await _append_chat_message(project_id, entry_final)
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
            v for _, v in form.multi_items() if isinstance(v, UploadFile)
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
        _stream_opencode(
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
    server = _opencode_servers.get(name)
    if not server:
        return {"processing": False}
    # Check if any session is busy
    try:
        oc_sid = _active_sessions.get(name)
        if oc_sid:
            resp = await server["client"].get("/session/status")
            statuses = resp.json()
            for s in statuses:
                if (
                    s.get("sessionID") == oc_sid
                    and s.get("status", {}).get("type") == "busy"
                ):
                    return {"processing": True}
    except Exception:
        pass
    return {"processing": False}


@router.get("/{name}/chat/history")
async def get_chat_history(name: str, user: dict = Depends(get_current_user)):
    project = await _get_project_for_user(user, name)
    messages = await _load_chat_messages(project["id"])
    return {"messages": messages}
