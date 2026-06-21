"""T7.2 슬라이스 4 — POST /builder/okf/commit 결정론 테스트.

TestClient + 세션 + FakeGitGateway 주입. 실 git·실 claude·실 네트워크 0.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.git_gateway import FakeGitGateway
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_SECRET = "test-secret"

_VALID_FILES = [{"path": "policy.md", "content": "# 환불\n\n내용"}]


def _make_client(gw: FakeGitGateway | None = None) -> TestClient:
    gw = gw or FakeGitGateway()
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET, git_gateway=gw)
    return TestClient(app)


def _login(client: TestClient, user_id: str) -> None:
    http: Any = client
    res: Response = cast(Response, http.post("/login", json={"user_id": user_id}))
    assert res.status_code == 200, f"로그인 실패: {user_id}"


def _commit(
    client: TestClient,
    agent_id: str = "cs_ops",
    files: list[dict[str, str]] | None = None,
    message: str = "정책 갱신",
) -> tuple[int, Any]:
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "files": files if files is not None else _VALID_FILES,
        "message": message,
    }
    http: Any = client
    res: Response = cast(Response, http.post("/builder/okf/commit", json=payload))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


class TestBuilderOkfCommit인증:
    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _commit(client)
        assert status == 401

    def test_로그인_후_커밋_200_sha_반환(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, body = _commit(client, agent_id="cs_ops")
        assert status == 200
        assert "sha" in body
        assert body["sha"] != ""
        assert body["agent_id"] == "cs_ops"


class TestBuilderOkfCommit스코프:
    def test_세션_불일치_403(self) -> None:
        """cs_lead로 로그인 후 contract_ops(owner=legal_lead) 커밋 시도 → 403."""
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _commit(client, agent_id="contract_ops")
        assert status == 403

    def test_자기_번들_커밋_200(self) -> None:
        client = _make_client()
        _login(client, "legal_lead")
        status, _ = _commit(client, agent_id="contract_ops")
        assert status == 200

    def test_미존재_카드_404(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _commit(client, agent_id="no_such_card")
        assert status == 404

    def test_author는_세션_신원으로_채워진다(self) -> None:
        gw = FakeGitGateway()
        client = _make_client(gw)
        _login(client, "cs_lead")
        _commit(client, agent_id="cs_ops")
        req = gw._requests["cs_ops"][0]  # pyright: ignore[reportPrivateUsage]
        assert req.author == "cs_lead"


class TestBuilderOkfCommit입력검증:
    def test_빈_파일_리스트_400(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _commit(client, agent_id="cs_ops", files=[])
        assert status == 400

    def test_경로_탈출_400(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        bad_files = [{"path": "../escape.md", "content": "위험"}]
        status, _ = _commit(client, agent_id="cs_ops", files=bad_files)
        assert status == 400

    def test_커밋_결과_sha_결정론(self) -> None:
        """같은 시퀀스면 SHA가 항상 동일(결정론)."""
        gw1 = FakeGitGateway()
        gw2 = FakeGitGateway()
        c1 = _make_client(gw1)
        c2 = _make_client(gw2)
        _login(c1, "cs_lead")
        _login(c2, "cs_lead")
        _, b1 = _commit(c1, agent_id="cs_ops")
        _, b2 = _commit(c2, agent_id="cs_ops")
        assert b1["sha"] == b2["sha"]

    def test_message_전달(self) -> None:
        gw = FakeGitGateway()
        client = _make_client(gw)
        _login(client, "cs_lead")
        _commit(client, agent_id="cs_ops", message="환불 정책 갱신 v2")
        req = gw._requests["cs_ops"][0]  # pyright: ignore[reportPrivateUsage]
        assert req.message == "환불 정책 갱신 v2"

    def test_빈_agent_id_400(self) -> None:
        """m3: 빈 agent_id는 400 — validate_card_for_builder와 대칭."""
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _commit(client, agent_id="")
        assert status == 400

    def test_공백_agent_id_400(self) -> None:
        """m3: 공백만인 agent_id도 400."""
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _commit(client, agent_id="   ")
        assert status == 400
