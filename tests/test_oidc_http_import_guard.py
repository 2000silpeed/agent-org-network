"""T8.3 — HttpOidcProvider extra 미설치 가드 단위 테스트.

oidc extra(PyJWT)가 없는 환경을 `import jwt` 실패로 시뮬해, 생성자가 조용히 폴백하지 않고
명확한 SystemExit으로 안내하는지 단언한다(`FastEmbedEmbedder`/`select_author`의 SystemExit
계약과 같은 정신 — 실사용 가짜 0·조용한 폴백 없음).

실 게이트 환경엔 dev 그룹으로 PyJWT가 있으므로, jwt import를 monkeypatch로 강제 실패시켜
미설치 분기를 결정론으로 탄다(builtins.__import__ 훅).
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from agent_org_network.oidc import HttpOidcProvider


def test_extra_미설치면_생성_시_SystemExit(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jwt":
            raise ImportError("No module named 'jwt' (시뮬)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SystemExit):
        HttpOidcProvider(
            issuer="https://idp.example.com",
            audience="agent-org-app",
            jwks_url="https://idp.example.com/jwks",
        )
