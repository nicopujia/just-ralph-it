import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.auth_utils import get_current_user
from app.config import DATA_DIR
from app.database import get_db

router = APIRouter(prefix="/api/projects", tags=["uploads"])


async def _get_project_dir(name: str, user: dict) -> Path:
    """Verify the project belongs to the user and return its directory path."""
    user_id: int = user["id"]
    github_username: str = user["github_username"]

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")

    return DATA_DIR / github_username / name


def _has_path_traversal(filename: str) -> bool:
    """Return True if the filename contains path traversal sequences."""
    return ".." in filename or "/" in filename


def _resolve_collision(directory: Path, filename: str) -> str:
    """Return a filename that doesn't collide with existing files.

    file.txt -> file_1.txt -> file_2.txt, etc.
    """
    if not (directory / filename).exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if not (directory / candidate).exists():
            return candidate
        counter += 1


def _get_upload_path(project_dir: Path, filename: str) -> Path:
    """Validate and return the path for an uploaded file."""
    if _has_path_traversal(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = project_dir / ".ralph" / "uploads" / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return file_path


class RenameRequest(BaseModel):
    new_name: str


@router.get("/{name}/uploads")
async def list_uploads(name: str, user: dict = Depends(get_current_user)):
    project_dir = await _get_project_dir(name, user)
    uploads_dir = project_dir / ".ralph" / "uploads"

    if not uploads_dir.exists():
        return []

    files = []
    for entry in uploads_dir.iterdir():
        if entry.is_file():
            stat = entry.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            files.append(
                {
                    "name": entry.name,
                    "size": stat.st_size,
                    "modified_at": modified_at.isoformat(),
                }
            )

    files.sort(key=lambda f: f["modified_at"], reverse=True)
    return files


@router.post("/{name}/uploads")
async def upload_file(
    name: str,
    file: UploadFile,
    user: dict = Depends(get_current_user),
):
    project_dir = await _get_project_dir(name, user)
    uploads_dir = project_dir / ".ralph" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    actual_name = _resolve_collision(uploads_dir, str(file.filename or "upload"))
    dest = uploads_dir / actual_name

    content = await file.read()
    dest.write_bytes(content)

    return {"name": actual_name, "size": len(content)}


@router.get("/{name}/uploads/{filename}")
async def get_upload(
    name: str,
    filename: str,
    user: dict = Depends(get_current_user),
):
    project_dir = await _get_project_dir(name, user)
    file_path = _get_upload_path(project_dir, filename)
    media_type, _ = mimetypes.guess_type(str(file_path))
    headers = {"Content-Disposition": f'inline; filename="{file_path.name}"'}
    return FileResponse(file_path, media_type=media_type, headers=headers)


@router.delete("/{name}/uploads/{filename}")
async def delete_upload(
    name: str,
    filename: str,
    user: dict = Depends(get_current_user),
):
    project_dir = await _get_project_dir(name, user)
    file_path = _get_upload_path(project_dir, filename)

    file_path.unlink()
    return JSONResponse(status_code=204, content=None)


@router.patch("/{name}/uploads/{filename}")
async def rename_upload(
    name: str,
    filename: str,
    body: RenameRequest,
    user: dict = Depends(get_current_user),
):
    if _has_path_traversal(body.new_name):
        raise HTTPException(status_code=400, detail="Invalid filename")

    project_dir = await _get_project_dir(name, user)
    uploads_dir = project_dir / ".ralph" / "uploads"
    file_path = _get_upload_path(project_dir, filename)

    actual_name = _resolve_collision(uploads_dir, body.new_name)
    file_path.rename(uploads_dir / actual_name)

    return {"name": actual_name}
