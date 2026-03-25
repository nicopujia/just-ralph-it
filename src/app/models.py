from pydantic import BaseModel
from typing import Optional


class UserOut(BaseModel):
    id: int
    github_id: int
    github_login: str
    github_name: Optional[str] = None
    github_avatar_url: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    repo_url: Optional[str] = None


class ProjectOut(BaseModel):
    id: int
    user_id: int
    name: str
    description: Optional[str] = None
    repo_url: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    project_id: int
    message: str


class ChatResponse(BaseModel):
    reply: str
