import base64
import json
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, cast, override

import httpx
from openai import DefaultHttpxClient, OpenAI
from openai._models import FinalRequestOptions

__all__ = ["Auth", "AuthError", "Client"]

file_lock: Any = import_module("msvcrt" if sys.platform == "win32" else "fcntl")


class AuthError(Exception): ...


class Auth(httpx.Auth):
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    OAUTH_URL = "https://auth.openai.com/oauth/token"

    def __init__(self, originator: str) -> None:
        # Naming the application refreshing the login lets concurrent
        # applications lock the login file under their own name.
        self.originator = originator
        self.path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        self.lock = Lock()

    @override
    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        credentials = self._read_credentials()
        request.headers["Authorization"] = f"Bearer {credentials['access_token']}"
        request.headers["chatgpt-account-id"] = credentials["account_id"]
        response = yield request
        if response.status_code != httpx.codes.UNAUTHORIZED:
            return

        credentials = self._refresh(credentials["refresh_token"])
        request.headers["Authorization"] = f"Bearer {credentials['access_token']}"
        request.headers["chatgpt-account-id"] = credentials["account_id"]
        yield request

    def validate(self) -> None:
        self._read_credentials()

    def _read_credentials(self) -> dict[str, str]:
        data = self._read()
        tokens = data.get("tokens")
        if data.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict):
            raise AuthError("Codex is not logged in with ChatGPT. Run `codex login`.")
        try:
            credentials = {key: tokens[key] for key in ("access_token", "refresh_token", "account_id")}
        except KeyError as error:
            raise AuthError("The Codex login is incomplete. Run `codex login` again.") from error
        if not all(isinstance(value, str) and value for value in credentials.values()):
            raise AuthError("The Codex login is incomplete. Run `codex login` again.")
        if self._has_expired(credentials["access_token"]):
            return self._refresh(credentials["refresh_token"])
        return credentials

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError as error:
            raise AuthError(
                'No file-based Codex login found. Set `cli_auth_credentials_store = "file"` and run `codex login`.'
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise AuthError("The Codex login cannot be read. Run `codex login` again.") from error
        if not isinstance(data, dict):
            raise AuthError("The Codex login is invalid. Run `codex login` again.")
        return data

    @staticmethod
    def _has_expired(access_token: str) -> bool:
        try:
            payload = access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            expires = json.loads(base64.urlsafe_b64decode(payload))["exp"]
            return datetime.now(tz=UTC).timestamp() >= float(expires) - 30
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return True

    def _refresh(self, refresh_token: str) -> dict[str, str]:
        with self.lock, self._file_lock():
            current = self._read()
            tokens = current.get("tokens", {})
            if isinstance(tokens, dict) and tokens.get("refresh_token") != refresh_token:
                access_token = tokens.get("access_token")
                account_id = tokens.get("account_id")
                new_refresh_token = tokens.get("refresh_token")
                values = (access_token, account_id, new_refresh_token)
                if all(isinstance(value, str) for value in values) and all(values):
                    access_token, account_id, new_refresh_token = cast("tuple[str, str, str]", values)
                    if not self._has_expired(access_token):
                        return {
                            "access_token": access_token,
                            "refresh_token": new_refresh_token,
                            "account_id": account_id,
                        }
                    refresh_token = new_refresh_token
            try:
                response = httpx.post(
                    self.OAUTH_URL,
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self.CLIENT_ID},
                    timeout=20,
                )
                response.raise_for_status()
                refreshed = response.json()
                access_token = refreshed["access_token"]
                new_refresh_token = refreshed["refresh_token"]
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                raise AuthError("The Codex login expired. Run `codex login` again.") from error

            account_id = tokens.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise AuthError("The Codex login is incomplete. Run `codex login` again.")
            tokens.update({"access_token": access_token, "refresh_token": new_refresh_token})
            current["tokens"] = tokens
            current["last_refresh"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
            self._write(current)
            return {"access_token": access_token, "refresh_token": new_refresh_token, "account_id": account_id}

    @contextmanager
    def _file_lock(self) -> Generator[None]:
        lock_path = self.path.with_suffix(f".{self.originator}.lock")
        with lock_path.open("w") as lock_file:
            if sys.platform == "win32":
                file_lock.locking(lock_file.fileno(), file_lock.LK_LOCK, 1)
            else:
                file_lock.flock(lock_file, file_lock.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    lock_file.seek(0)
                    file_lock.locking(lock_file.fileno(), file_lock.LK_UNLCK, 1)
                else:
                    file_lock.flock(lock_file, file_lock.LOCK_UN)

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as file:
                file.write(f"{json.dumps(data, indent=2)}\n")
            Path(file.name).chmod(0o600)
            Path(file.name).replace(self.path)
        except (OSError, TypeError, ValueError) as error:
            raise AuthError("The refreshed Codex login could not be saved.") from error


class Client(OpenAI):
    def __init__(self, originator: str) -> None:
        super().__init__(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="codex",
            default_headers={"originator": originator, "OpenAI-Beta": "responses=experimental"},
            http_client=DefaultHttpxClient(auth=Auth(originator)),
        )

    @override
    def _prepare_options(self, options: FinalRequestOptions) -> FinalRequestOptions:
        if options.url == "/responses" and isinstance(options.json_data, dict):
            body = dict(options.json_data)
            context = body.get("input")
            if isinstance(context, list) and context and context[0].get("role") == "system":
                body["instructions"] = context[0]["content"]
                body["input"] = context[1:]
            body.pop("temperature", None)
            body.update(store=False, include=["reasoning.encrypted_content"])
            options.json_data = body
        return super()._prepare_options(options)
