from contextlib import asynccontextmanager

import aiosqlite

from app.config import DATA_DIR

DATABASE_PATH = DATA_DIR / "jri.db"


@asynccontextmanager
async def get_db():
    """Async context manager yielding an aiosqlite connection."""
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create database tables if they don't already exist. Idempotent."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                github_id INTEGER UNIQUE NOT NULL,
                github_username TEXT NOT NULL,
                github_name TEXT,
                github_email TEXT,
                github_avatar_url TEXT,
                github_token TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                github_repo_url TEXT,
                ralph_session_id TEXT,
                ralph_loop_status TEXT NOT NULL DEFAULT 'idle',
                ralph_loop_current_issue TEXT,
                ralph_loop_iteration INTEGER NOT NULL DEFAULT 0,
                stripe_payment_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                task_id TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                thinking_text TEXT DEFAULT '',
                thinking_steps TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Migrate: add subscription columns to users
        for col_name, col_def in [
            ("subscription_plan", "TEXT DEFAULT NULL"),
            ("subscription_id", "TEXT DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass  # column already exists

        # Migrate: rename beads_issue_id -> task_id in notifications
        try:
            await db.execute(
                "ALTER TABLE notifications RENAME COLUMN beads_issue_id TO task_id"
            )
        except Exception:
            pass  # column already renamed or doesn't exist

        # Migrate: add deployment columns to projects if they don't exist.
        # SQLite doesn't support IF NOT EXISTS for ALTER TABLE, so catch errors.
        _deploy_columns = [
            ("deploy_type", "TEXT DEFAULT NULL"),
            ("deploy_port", "INTEGER DEFAULT NULL"),
            ("deploy_status", "TEXT DEFAULT 'idle'"),
            ("deploy_start_command", "TEXT DEFAULT NULL"),
            ("deploy_subdomain", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in _deploy_columns:
            try:
                await db.execute(
                    f"ALTER TABLE projects ADD COLUMN {col_name} {col_def}"
                )
            except Exception:
                pass  # column already exists

        # Migrate: add paid_task_count column to projects
        try:
            await db.execute(
                "ALTER TABLE projects ADD COLUMN paid_task_count INTEGER DEFAULT 0"
            )
        except Exception:
            pass  # column already exists

        # Migrate: add base_fee_paid column to projects
        try:
            await db.execute(
                "ALTER TABLE projects ADD COLUMN base_fee_paid INTEGER DEFAULT 0"
            )
        except Exception:
            pass  # column already exists

        # Migrate: add role column to users
        try:
            await db.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        except Exception:
            pass  # column already exists

        # Migrate: seed roles from legacy whitelist.txt / freelist.txt
        # Only runs if there are users with no role set (i.e. still 'user' default).
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE role != 'user'")
        (role_count,) = await cursor.fetchone()
        if role_count == 0:
            _whitelist_path = DATA_DIR / "whitelist.txt"
            _freelist_path = DATA_DIR / "freelist.txt"
            _whitelist: set[str] = set()
            _freelist: set[str] = set()
            if _whitelist_path.exists():
                _whitelist = {
                    ln.strip()
                    for ln in _whitelist_path.read_text().splitlines()
                    if ln.strip()
                }
            if _freelist_path.exists():
                _freelist = {
                    ln.strip()
                    for ln in _freelist_path.read_text().splitlines()
                    if ln.strip()
                }
            # admin
            await db.execute(
                "UPDATE users SET role = 'admin' WHERE github_username = 'nicopujia'"
            )
            # free (freelist minus admin)
            for uname in _freelist - {"nicopujia"}:
                await db.execute(
                    "UPDATE users SET role = 'free' WHERE github_username = ?",
                    (uname,),
                )
            # beta (whitelist minus freelist minus admin)
            for uname in _whitelist - _freelist - {"nicopujia"}:
                await db.execute(
                    "UPDATE users SET role = 'beta' WHERE github_username = ?",
                    (uname,),
                )

        await db.commit()
