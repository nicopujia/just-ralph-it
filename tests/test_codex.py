import json
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import openai
import pytest

from jri.lib.providers import codex
from tests.doubles.codex import (
    DISTANT_FUTURE,
    NOW,
    ORIGINATOR,
    FakeProvider,
    FrozenClock,
    build_client,
    build_token,
    deny_replacement,
    encode_token,
    respond,
    retry_after_rejection,
    write_login,
)

REFRESHED_EXPIRY = DISTANT_FUTURE + 3600


@pytest.fixture(autouse=True)
def isolate_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))


def test_accepts_a_complete_unexpired_login(tmp_path: Path) -> None:
    write_login(
        tmp_path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )

    codex.Auth(ORIGINATOR).validate()


def test_reports_a_missing_login(tmp_path: Path) -> None:
    with pytest.raises(codex.AuthError, match="No file-based Codex login found"):
        codex.Auth(ORIGINATOR).validate()

    assert not (tmp_path / "auth.json").exists()


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        ("not json", "cannot be read"),
        ("[]", "is invalid"),
        ('{"auth_mode": "apikey", "tokens": {}}', "not logged in with ChatGPT"),
        ('{"auth_mode": "chatgpt"}', "not logged in with ChatGPT"),
        ('{"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": "r"}}', "incomplete"),
        (
            '{"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": "r", "account_id": ""}}',
            "incomplete",
        ),
        (
            '{"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": "r", "account_id": 7}}',
            "incomplete",
        ),
    ],
    ids=["malformed-json", "not-an-object", "api-key-mode", "no-tokens", "missing-account", "blank", "wrong-type"],
)
def test_reports_an_unusable_login(tmp_path: Path, contents: str, reason: str) -> None:
    (tmp_path / "auth.json").write_text(contents)

    with pytest.raises(codex.AuthError, match=reason):
        codex.Auth(ORIGINATOR).validate()


def test_reports_a_login_that_is_not_utf_8(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_bytes(b'{"auth_mode": "chatgpt\xff"}')

    with pytest.raises(codex.AuthError, match="cannot be read"):
        codex.Auth(ORIGINATOR).validate()


def test_reports_a_login_the_filesystem_refuses_to_hand_over(tmp_path: Path) -> None:
    (tmp_path / "auth.json").mkdir()

    with pytest.raises(codex.AuthError, match="cannot be read"):
        codex.Auth(ORIGINATOR).validate()


@pytest.mark.parametrize(
    "access_token",
    ["opaque", "header.!!.signature", encode_token("not json"), encode_token("{}"), encode_token('{"exp": null}')],
    ids=["no-segments", "undecodable-payload", "unreadable-payload", "no-expiry", "null-expiry"],
)
def test_refreshes_a_login_whose_expiry_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access_token: str
) -> None:
    write_login(tmp_path, {"access_token": access_token, "refresh_token": "refresh", "account_id": "account"})
    refreshed = {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "next-refresh"}
    provider = FakeProvider(respond(200, refreshed))
    monkeypatch.setattr(codex.httpx, "post", provider.post)

    codex.Auth(ORIGINATOR).validate()

    assert len(provider.calls) == 1
    assert json.loads((tmp_path / "auth.json").read_text())["tokens"] == {**refreshed, "account_id": "account"}


@pytest.mark.parametrize(
    ("expires", "refreshes"), [(NOW + 30, 1), (NOW + 31, 0)], ids=["within-the-skew", "beyond-the-skew"]
)
# JRI reads a token as expired 30 seconds before it expires.
# A request that starts just before that time does not fail while it runs.
# This test states the exact boundary, so a change to those 30 seconds fails it.
def test_treats_a_login_about_to_expire_as_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expires: int, refreshes: int
) -> None:
    write_login(tmp_path, {"access_token": build_token(expires), "refresh_token": "refresh", "account_id": "account"})
    provider = FakeProvider(respond(200, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "next"}))
    monkeypatch.setattr(codex.httpx, "post", provider.post)
    monkeypatch.setattr(codex, "datetime", FrozenClock)

    codex.Auth(ORIGINATOR).validate()

    assert len(provider.calls) == refreshes


def test_reports_a_provider_that_cannot_be_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    login = {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"}
    write_login(tmp_path, login)
    monkeypatch.setattr(codex.httpx, "post", FakeProvider(httpx.ConnectError("unreachable")).post)

    with pytest.raises(codex.AuthError, match="expired"):
        codex.Auth(ORIGINATOR).validate()

    assert json.loads((tmp_path / "auth.json").read_text())["tokens"] == login


@pytest.mark.parametrize("body", ["not json", "[]"], ids=["malformed-json", "not-an-object"])
def test_reports_a_refresh_the_provider_answered_unusably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    write_login(tmp_path, {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"})
    monkeypatch.setattr(codex.httpx, "post", FakeProvider(respond(200, body)).post)

    with pytest.raises(codex.AuthError, match="expired"):
        codex.Auth(ORIGINATOR).validate()


def test_refreshes_an_expired_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_login(tmp_path, {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"})
    refreshed = {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "next-refresh"}
    provider = FakeProvider(respond(200, refreshed))
    monkeypatch.setattr(codex.httpx, "post", provider.post)

    codex.Auth(ORIGINATOR).validate()

    # The expired login holds one refresh token, and a refresh spends it once.
    assert [data["refresh_token"] for _, data in provider.calls] == ["refresh"]
    stored = json.loads((tmp_path / "auth.json").read_text())
    assert stored["tokens"] == {**refreshed, "account_id": "account"}
    assert stored["last_refresh"].endswith("Z")


def test_reports_an_expired_login_that_the_provider_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    login = {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"}
    write_login(tmp_path, login)
    rejection = respond(400, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "next-refresh"})
    monkeypatch.setattr(codex.httpx, "post", FakeProvider(rejection).post)

    with pytest.raises(codex.AuthError, match="expired"):
        codex.Auth(ORIGINATOR).validate()

    assert json.loads((tmp_path / "auth.json").read_text())["tokens"] == login


def test_reports_an_expired_login_whose_refresh_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_login(tmp_path, {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"})
    monkeypatch.setattr(codex.httpx, "post", FakeProvider(respond(200, {"access_token": "only"})).post)

    with pytest.raises(codex.AuthError, match="expired"):
        codex.Auth(ORIGINATOR).validate()


def test_retries_a_rejected_request_with_a_refreshed_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    refreshed = {"access_token": build_token(REFRESHED_EXPIRY), "refresh_token": "next-refresh"}
    provider = FakeProvider(respond(200, refreshed))
    monkeypatch.setattr(codex.httpx, "post", provider.post)
    client = build_client(tmp_path, monkeypatch, requests, statuses=[httpx.codes.UNAUTHORIZED])

    client.responses.with_raw_response.create(model="gpt-5.6-sol", input="Hello.")

    # The provider rejects a request that names no account.
    # Each attempt must send the account id beside the token that it uses.
    assert [(request.headers["Authorization"], request.headers["chatgpt-account-id"]) for request in requests] == [
        (f"Bearer {build_token(DISTANT_FUTURE)}", "account"),
        (f"Bearer {refreshed['access_token']}", "account"),
    ]
    # One rejection asks for one refresh. A second would spend a token the provider already replaced.
    assert len(provider.calls) == 1
    assert json.loads((tmp_path / "auth.json").read_text())["tokens"] == {**refreshed, "account_id": "account"}


def test_adopts_the_login_a_sibling_process_refreshed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    sibling = {"access_token": build_token(REFRESHED_EXPIRY), "refresh_token": "sibling-refresh"}
    provider = FakeProvider(respond(200, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "burned"}))
    monkeypatch.setattr(codex.httpx, "post", provider.post)
    client = build_client(
        tmp_path,
        monkeypatch,
        requests,
        statuses=[httpx.codes.UNAUTHORIZED],
        on_first_request=lambda: write_login(tmp_path, {**sibling, "account_id": "account"}),
    )

    client.responses.with_raw_response.create(model="gpt-5.6-sol", input="Hello.")

    # A refresh token works one time only.
    # The provider would also reject this process if it sent the token that a sibling spent.
    assert provider.calls == []
    assert requests[1].headers["Authorization"] == f"Bearer {sibling['access_token']}"
    assert json.loads((tmp_path / "auth.json").read_text())["tokens"] == {**sibling, "account_id": "account"}


def test_refreshes_with_the_token_a_sibling_process_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    refreshed = {"access_token": build_token(REFRESHED_EXPIRY), "refresh_token": "next-refresh"}
    provider = FakeProvider(respond(200, refreshed))
    monkeypatch.setattr(codex.httpx, "post", provider.post)
    client = build_client(
        tmp_path,
        monkeypatch,
        requests,
        statuses=[httpx.codes.UNAUTHORIZED],
        on_first_request=lambda: write_login(
            tmp_path, {"access_token": build_token(0), "refresh_token": "sibling-refresh", "account_id": "account"}
        ),
    )

    client.responses.with_raw_response.create(model="gpt-5.6-sol", input="Hello.")

    assert provider.calls == [
        (
            codex.Auth.OAUTH_URL,
            {"grant_type": "refresh_token", "refresh_token": "sibling-refresh", "client_id": codex.Auth.CLIENT_ID},
        )
    ]
    assert requests[1].headers["Authorization"] == f"Bearer {refreshed['access_token']}"


@pytest.mark.parametrize(
    "sibling",
    [
        {"access_token": "", "refresh_token": "sibling-refresh", "account_id": "account"},
        {"refresh_token": "sibling-refresh", "account_id": "account"},
    ],
    ids=["blank-access-token", "missing-access-token"],
)
def test_refreshes_with_its_own_token_when_the_sibling_login_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sibling: dict[str, Any]
) -> None:
    requests: list[httpx.Request] = []
    refreshed = {"access_token": build_token(REFRESHED_EXPIRY), "refresh_token": "next-refresh"}
    provider = FakeProvider(respond(200, refreshed))
    monkeypatch.setattr(codex.httpx, "post", provider.post)
    client = build_client(
        tmp_path,
        monkeypatch,
        requests,
        statuses=[httpx.codes.UNAUTHORIZED],
        on_first_request=lambda: write_login(tmp_path, sibling),
    )

    client.responses.with_raw_response.create(model="gpt-5.6-sol", input="Hello.")

    assert provider.calls == [
        (
            codex.Auth.OAUTH_URL,
            {"grant_type": "refresh_token", "refresh_token": "refresh", "client_id": codex.Auth.CLIENT_ID},
        )
    ]
    assert requests[1].headers["Authorization"] == f"Bearer {refreshed['access_token']}"


def test_reports_a_login_a_sibling_process_replaced_with_a_malformed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_login(
        tmp_path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )
    provider = FakeProvider(respond(200, {"access_token": build_token(REFRESHED_EXPIRY), "refresh_token": "next"}))
    monkeypatch.setattr(codex.httpx, "post", provider.post)

    with pytest.raises(codex.AuthError, match="is invalid"):
        retry_after_rejection(codex.Auth(ORIGINATOR), lambda: write_login(tmp_path, ["not", "an", "object"]))

    # A refresh token works one time only.
    # JRI must not spend it on a login that JRI then rejects.
    # The file would keep the token that the provider replaced, and no refresh could repair that login.
    assert provider.calls == []


def test_reports_a_refresh_the_sibling_process_left_without_an_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_login(
        tmp_path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )
    sibling = {"access_token": build_token(0), "refresh_token": "refresh"}
    provider = FakeProvider(respond(200, {"access_token": build_token(REFRESHED_EXPIRY), "refresh_token": "next"}))
    monkeypatch.setattr(codex.httpx, "post", provider.post)

    with pytest.raises(codex.AuthError, match="incomplete"):
        retry_after_rejection(codex.Auth(ORIGINATOR), lambda: write_login(tmp_path, sibling))

    # The account id must permit the write.
    # Without it, JRI would replace the record of the sibling with new tokens that are also incomplete.
    # JRI must leave that record for a later, valid refresh to repair.
    assert json.loads((tmp_path / "auth.json").read_text())["tokens"] == sibling


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a directory that refuses a write is an access list on Windows, and `chmod` writes no list",
)
def test_reports_a_refreshed_login_that_cannot_be_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "read-only"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    write_login(home, {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"})
    (home / f"auth.{ORIGINATOR}.lock").touch()
    monkeypatch.setattr(
        codex.httpx,
        "post",
        FakeProvider(respond(200, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "next"})).post,
    )
    home.chmod(0o500)

    try:
        with pytest.raises(codex.AuthError, match="could not be saved"):
            codex.Auth(ORIGINATOR).validate()
    finally:
        home.chmod(0o700)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a directory that refuses a write is an access list on Windows, and `chmod` writes no list",
)
def test_reports_a_refresh_whose_lock_cannot_be_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "read-only"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    write_login(home, {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"})
    home.chmod(0o500)

    try:
        with pytest.raises(codex.AuthError, match="could not be locked"):
            codex.Auth(ORIGINATOR).validate()
    finally:
        home.chmod(0o700)


def test_leaves_no_scratch_file_behind_when_saving_the_login_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_login(tmp_path, {"access_token": build_token(0), "refresh_token": "refresh", "account_id": "account"})
    monkeypatch.setattr(
        codex.httpx,
        "post",
        FakeProvider(respond(200, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "next"})).post,
    )
    deny_replacement(monkeypatch)

    with pytest.raises(codex.AuthError, match="could not be saved"):
        codex.Auth(ORIGINATOR).validate()

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["auth.json", f"auth.{ORIGINATOR}.lock"]


def test_stops_asking_for_a_login_after_a_second_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    refreshed = build_token(REFRESHED_EXPIRY)
    provider = FakeProvider(respond(200, {"access_token": refreshed, "refresh_token": "next-refresh"}))
    monkeypatch.setattr(codex.httpx, "post", provider.post)
    rejections = [httpx.codes.UNAUTHORIZED, httpx.codes.UNAUTHORIZED]
    client = build_client(tmp_path, monkeypatch, requests, statuses=rejections)

    # JRI retries one time only.
    # An account with a login that is truly broken thus fails immediately.
    # More retries would send many requests to the token endpoint and get nothing.
    with pytest.raises(openai.AuthenticationError, match="Error code: 401"):
        client.responses.with_raw_response.create(model="gpt-5.6-sol", input="Hello.")

    assert [request.headers["Authorization"] for request in requests] == [
        f"Bearer {build_token(DISTANT_FUTURE)}",
        f"Bearer {refreshed}",
    ]
    assert len(provider.calls) == 1


def test_adapts_responses_requests_to_the_codex_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    client = build_client(tmp_path, monkeypatch, requests)

    client.responses.with_raw_response.create(
        model="gpt-5.6-sol",
        input=[{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Hello."}],
        temperature=0.7,
    )

    assert json.loads(requests[0].content) == {
        "input": [{"role": "user", "content": "Hello."}],
        "instructions": "Be terse.",
        "model": "gpt-5.6-sol",
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }


def test_leaves_a_context_without_a_system_message_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    context: list[Any] = [{"role": "user", "content": "Hello."}, {"role": "system", "content": "Be terse."}]
    client = build_client(tmp_path, monkeypatch, requests)

    client.responses.with_raw_response.create(model="gpt-5.6-sol", input=context)

    assert json.loads(requests[0].content) == {
        "input": context,
        "model": "gpt-5.6-sol",
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }


def test_leaves_other_requests_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    client = build_client(tmp_path, monkeypatch, requests)

    client.embeddings.with_raw_response.create(
        model="text-embedding-3-small", input="Hello.", extra_body={"temperature": 0.7}
    )

    body = cast("dict[str, Any]", json.loads(requests[0].content))
    assert (body["model"], body["input"], body["temperature"]) == ("text-embedding-3-small", "Hello.", 0.7)
    assert {"instructions", "store", "include"}.isdisjoint(body)
