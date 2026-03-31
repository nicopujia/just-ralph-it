"""Pydantic models for API request/response schemas."""

from pydantic import BaseModel, Field

# --- User models ---


class User(BaseModel):
    """Authenticated user from session."""

    id: int
    github_id: int
    github_username: str
    github_name: str | None = None
    github_email: str | None = None
    github_avatar_url: str | None = None
    github_token: str | None = None
    role: str | None = None
    subscription_plan: str | None = None
    impersonating_from: int | None = None


class UserPublic(BaseModel):
    """Public user info returned by /auth/me."""

    id: int
    github_username: str
    github_name: str | None = None
    github_avatar_url: str | None = None
    role: str = "user"


# --- Task models ---


class Task(BaseModel):
    """Task data from .jri/tasks/."""

    id: str
    title: str | None = None
    status: str
    priority: int = 4
    depends_on: list[str] = Field(default_factory=list)
    description: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)

    model_config = {"extra": "allow"}


# --- Project models ---


class CreateProjectRequest(BaseModel):
    name: str
    description: str


class ProjectCreated(BaseModel):
    id: int
    name: str
    description: str
    github_repo_url: str
    deploy_port: int
    deploy_subdomain: str


class ProjectSummary(BaseModel):
    """Project info for list view."""

    id: int
    name: str
    description: str | None = None
    github_repo_url: str | None = None
    issue_count: int = 0
    ralph_loop_status: str | None = None
    created_at: str | None = None


class ProjectDetail(BaseModel):
    """Full project info."""

    id: int
    name: str
    description: str | None = None
    github_repo_url: str | None = None
    issue_count: int = 0
    ralph_session_id: str | None = None
    ralph_loop_status: str | None = None
    ralph_loop_current_issue: str | None = None
    ralph_loop_iteration: int | None = None
    deploy_type: str | None = None
    deploy_port: int | None = None
    deploy_status: str | None = None
    deploy_subdomain: str | None = None
    created_at: str | None = None


class EnvUpdateRequest(BaseModel):
    content: str


class EnvResponse(BaseModel):
    content: str


class EnvSavedResponse(BaseModel):
    status: str = "saved"


class ReadmeResponse(BaseModel):
    content: str
    exists: bool


class DeployRequest(BaseModel):
    start_command: str | None = None


class DeployResponse(BaseModel):
    subdomain_url: str
    port: int


class DeployStopResponse(BaseModel):
    deploy_status: str = "stopped"


# --- Chat models ---


class ChatRequest(BaseModel):
    message: str


class ChatProcessingResponse(BaseModel):
    processing: bool


class ChatMessage(BaseModel):
    role: str
    content: str
    thinkingText: str | None = None
    thinkingSteps: list[str] | None = None


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


# --- Upload models ---


class UploadInfo(BaseModel):
    name: str
    size: int
    modified_at: str | None = None


class UploadResponse(BaseModel):
    name: str
    size: int


class RenameRequest(BaseModel):
    new_name: str


class RenameResponse(BaseModel):
    name: str


# --- Ralph models ---


class RalphCheckoutResponse(BaseModel):
    free: bool
    redirect: str | None = None


class RalphPaymentCallbackResponse(BaseModel):
    status: str
    paid_task_count: int


class RalphStatusResponse(BaseModel):
    status: str


class RalphLoopStatusResponse(BaseModel):
    status: str
    current_issue: str | None = None
    iteration: int = 0
    recent_output: list[str] = Field(default_factory=list)


class RalphPaymentStatusResponse(BaseModel):
    paid_task_count: int
    total_tasks: int
    unpaid: int
    free_user: bool


class Notification(BaseModel):
    id: int
    message: str
    task_id: str | None = None
    created_at: str


class AcknowledgeResponse(BaseModel):
    status: str = "acknowledged"


class PricingResponse(BaseModel):
    per_task: int
    max_free_projects: int
    pro_monthly: int


# --- Error models ---


class ErrorResponse(BaseModel):
    detail: str


class UpgradeRequiredError(BaseModel):
    detail: str
    upgrade_required: bool = True
    plan: str
    price: str
