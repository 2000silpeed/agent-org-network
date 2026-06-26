"""T9.5(a) — TokenStore 포트 + 발급/검증/만료/revoke 결정 로직 테스트 (ADR 0026 결정 1·2·4).

결정론 보장:
  - 토큰 팩토리 주입(token_factory=)으로 raw_token 결정론.
  - 만료 판정은 주입 clock(now 파라미터).
  - 해시는 hashlib(결정론).
  - 비결정·외부 프로세스·네트워크 0.

잠근 불변식:
  - 평문 미저장 — 해시만 보관(DB 유출 시 평문 미노출).
  - 등록 무결성 — 만료/revoke/위조/없음 토큰은 verify None(admission 거부).
  - Authority 중앙 — 토큰은 중앙 발급·owner 귀속 선언이지 워커 자기보고 아님.
  - owner 격리 — 토큰의 owner_id 귀속.
  - 전이≠기록 — 발급/revoke는 도메인 admission 상태이지 audit 로그 아님.
  - clock 진전 시 만료.
  - revoke 멱등.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.token import AdmissionToken, InMemoryTokenStore, TokenStore


# ── 픽스처 ────────────────────────────────────────────────────────────────

T0 = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)

OWNER_ID = "owner_alice"
ROLE = "primary"


def make_store(token_seq: list[str] | None = None) -> InMemoryTokenStore:
    """결정론 token_factory 주입 — 리스트 순서대로 raw_token 반환."""
    tokens = list(token_seq or ["raw_token_1"])
    idx = 0

    def factory() -> str:
        nonlocal idx
        val = tokens[idx % len(tokens)]
        idx += 1
        return val

    return InMemoryTokenStore(token_factory=factory)


# ── Protocol 준수 ────────────────────────────────────────────────────────

def test_InMemoryTokenStore_는_TokenStore_프로토콜을_만족한다() -> None:
    """런타임 구조 검증 — TokenStore Protocol 메서드를 전부 구현한다."""
    store: TokenStore = InMemoryTokenStore()
    assert callable(store.issue)
    assert callable(store.verify)
    assert callable(store.revoke)
    assert callable(store.list_active)


# ── 발급(issue) ──────────────────────────────────────────────────────────

def test_issue_는_raw_token_과_AdmissionToken_을_반환한다() -> None:
    store = make_store(["tok_abc"])
    raw, token = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    assert raw == "tok_abc"
    assert isinstance(token, AdmissionToken)


def test_issue_는_owner_id_와_role_을_token_에_귀속한다() -> None:
    store = make_store()
    _, token = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    assert token.owner_id == OWNER_ID
    assert token.role == ROLE


def test_issue_는_평문을_저장하지_않고_해시만_보관한다() -> None:
    """평문 미저장 불변식: raw_token은 반환 후 store 내부에 없어야 한다."""
    store = make_store(["secret_raw"])
    raw, token = store.issue(OWNER_ID, ROLE, now=T0)

    # 평문이 token_hash에 그대로 들어가 있으면 안 됨
    assert token.token_hash != raw
    # token_hash는 비어있지 않아야 함
    assert len(token.token_hash) > 0


def test_issue_는_expires_at_을_now_기준으로_설정한다() -> None:
    store = make_store()
    _, token = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    assert token.expires_at == T1


def test_issue_는_expires_in_없으면_만료없음() -> None:
    store = make_store()
    _, token = store.issue(OWNER_ID, ROLE, now=T0)

    assert token.expires_at is None


def test_issue_는_issued_at_을_now_로_설정한다() -> None:
    store = make_store()
    _, token = store.issue(OWNER_ID, ROLE, now=T0)

    assert token.issued_at == T0


def test_issue_는_revoked_false_로_시작한다() -> None:
    store = make_store()
    _, token = store.issue(OWNER_ID, ROLE, now=T0)

    assert token.revoked is False
    assert token.revoked_at is None


def test_issue_는_token_id_를_자동_생성한다() -> None:
    store = make_store(["a", "b"])
    _, t1 = store.issue(OWNER_ID, ROLE, now=T0)
    _, t2 = store.issue(OWNER_ID, "backup", now=T0)

    assert t1.token_id != t2.token_id


def test_issue_는_서로_다른_raw_token_에_서로_다른_hash_를_생성한다() -> None:
    tokens = ["tok_a", "tok_b"]
    idx = 0

    def factory() -> str:
        nonlocal idx
        v = tokens[idx]
        idx += 1
        return v

    store = InMemoryTokenStore(token_factory=factory)
    _, t1 = store.issue(OWNER_ID, ROLE, now=T0)
    _, t2 = store.issue(OWNER_ID, ROLE, now=T0)

    assert t1.token_hash != t2.token_hash


# ── 검증(verify) ─────────────────────────────────────────────────────────

def test_verify_는_유효한_raw_token_으로_token_을_반환한다() -> None:
    store = make_store(["valid_tok"])
    raw, issued = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    result = store.verify(raw, now=T1 - timedelta(seconds=1))

    assert result is not None
    assert result.token_id == issued.token_id


def test_verify_는_위조_token_으로_None_을_반환한다() -> None:
    store = make_store()
    store.issue(OWNER_ID, ROLE, now=T0)

    result = store.verify("forged_token", now=T0)

    assert result is None


def test_verify_는_존재하지_않는_token_으로_None_을_반환한다() -> None:
    store = make_store()

    result = store.verify("no_such_token", now=T0)

    assert result is None


def test_verify_는_만료된_token_으로_None_을_반환한다() -> None:
    """clock 진전 시 만료 불변식."""
    store = make_store(["expiring"])
    raw, _ = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    # expires_at = T1. now = T1(경계) → 만료 판정
    result = store.verify(raw, now=T1)

    assert result is None


def test_verify_는_만료_직전_token_을_통과시킨다() -> None:
    store = make_store(["almost"])
    raw, _ = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    # now < expires_at → 유효
    result = store.verify(raw, now=T1 - timedelta(seconds=1))

    assert result is not None


def test_verify_는_만료없는_token_을_미래에도_통과시킨다() -> None:
    store = make_store(["no_exp"])
    raw, _ = store.issue(OWNER_ID, ROLE, now=T0)

    result = store.verify(raw, now=T2)

    assert result is not None


def test_verify_는_revoke된_token_으로_None_을_반환한다() -> None:
    """revoke 후 verify None — admission 거부."""
    store = make_store(["to_revoke"])
    raw, tok = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=2))

    store.revoke(tok.token_id)
    result = store.verify(raw, now=T1)

    assert result is None


# ── revoke ────────────────────────────────────────────────────────────────

def test_revoke_는_token_을_revoked_True_로_표식한다() -> None:
    """append-only revoke — Precedent.invalidated 패턴."""
    store = make_store()
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)

    revoked = store.revoke(tok.token_id)

    assert revoked is not None
    assert revoked.revoked is True
    assert revoked.revoked_at is not None


def test_revoke_는_token_을_삭제하지_않는다() -> None:
    """append-only: revoke 후 list_active에서만 빠짐, store에는 남음."""
    store = make_store()
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)

    store.revoke(tok.token_id)

    # list_active에는 없어야 하지만 내부 저장소에서 삭제 X
    # revoke 재호출이 가능해야 멱등 확인 가능
    result = store.revoke(tok.token_id)  # 두 번째 revoke
    assert result is not None  # None이 아니어야 함 (멱등)


def test_revoke_는_멱등이다() -> None:
    """동일 token_id 두 번 revoke → 같은 결과, 추가 변경 없음."""
    store = make_store()
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)

    r1 = store.revoke(tok.token_id)
    r2 = store.revoke(tok.token_id)

    assert r1 is not None
    assert r2 is not None
    assert r1.revoked is True
    assert r2.revoked is True


def test_revoke_는_없는_token_id_에_None_을_반환한다() -> None:
    store = make_store()

    result = store.revoke("nonexistent_id")

    assert result is None


def test_revoke_된_token_은_verify_에서_거부된다() -> None:
    """등록 무결성 — revoke 후 admission 거부."""
    store = make_store(["revoke_me"])
    raw, tok = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=2))

    store.revoke(tok.token_id)

    assert store.verify(raw, now=T1) is None


# ── list_active ───────────────────────────────────────────────────────────

def test_list_active_는_발급_후_token_을_포함한다() -> None:
    store = make_store()
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)

    active = store.list_active()

    assert any(t.token_id == tok.token_id for t in active)


def test_list_active_는_revoke_된_token_을_제외한다() -> None:
    store = make_store(["a", "b"])
    _, tok1 = store.issue(OWNER_ID, ROLE, now=T0)
    _, tok2 = store.issue("owner_bob", "backup", now=T0)

    store.revoke(tok1.token_id)

    active = store.list_active()
    ids = [t.token_id for t in active]
    assert tok1.token_id not in ids
    assert tok2.token_id in ids


def test_list_active_는_만료된_token_을_제외한다() -> None:
    """list_active는 현재 유효한(미만료·미revoke) 토큰만."""
    store = make_store(["exp"])

    # expires_in 1시간
    _, tok = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))

    # T1 기준 만료 → list_active(now=T1)는 제외
    active = store.list_active(now=T1)
    ids = [t.token_id for t in active]
    assert tok.token_id not in ids


def test_list_active_는_now_없으면_만료없는_token만_포함한다() -> None:
    """list_active now 없이 호출 — expires_at None 토큰은 active."""
    store = make_store(["no_exp"])
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)  # expires_in 없음 → 무기한

    active = store.list_active()
    assert any(t.token_id == tok.token_id for t in active)


# ── 불변식 적대 테스트 ─────────────────────────────────────────────────────

def test_평문은_token_hash_에_저장되지_않는다() -> None:
    """구조적 평문 미저장 — 해시 저장이 아니라 평문 저장이면 여기서 실패."""
    store = make_store(["super_secret_plaintext"])
    raw, tok = store.issue(OWNER_ID, ROLE, now=T0)

    assert tok.token_hash != raw
    # 해시는 hex 형태여야 함(hashlib)
    assert all(c in "0123456789abcdef" for c in tok.token_hash)


def test_다른_raw_token_은_검증을_통과하지_못한다() -> None:
    """owner 격리: 다른 토큰으로 검증 시도 → None."""
    store = make_store(["real_token"])
    _, _ = store.issue(OWNER_ID, ROLE, now=T0)

    result = store.verify("completely_different_token", now=T0)

    assert result is None


def test_owner_격리_token의_owner_id가_귀속된다() -> None:
    """Authority 중앙: 발급 시 owner_id가 중앙에서 귀속되고 자기보고가 아님."""
    store = make_store(["owner_tok"])
    raw, tok = store.issue("owner_x", ROLE, now=T0)

    assert tok.owner_id == "owner_x"

    # 다른 owner가 같은 raw_token을 가져도 verify 결과의 owner는 변하지 않음
    verified = store.verify(raw, now=T0)
    assert verified is not None
    assert verified.owner_id == "owner_x"


def test_frozen_AdmissionToken_은_변경_불가하다() -> None:
    """pydantic frozen 또는 dataclass frozen 검증."""
    store = make_store()
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)

    with pytest.raises((TypeError, AttributeError)):
        tok.revoked = True  # type: ignore[misc]


def test_AdmissionToken_은_frozen_dataclass_이다() -> None:
    """AdmissionToken이 frozen임을 타입 레벨에서 확인."""
    import dataclasses

    assert dataclasses.is_dataclass(AdmissionToken)
    fields = {f.name for f in dataclasses.fields(AdmissionToken)}
    assert "token_id" in fields
    assert "owner_id" in fields
    assert "role" in fields
    assert "token_hash" in fields
    assert "issued_at" in fields
    assert "expires_at" in fields
    assert "revoked" in fields
    assert "revoked_at" in fields


def test_전이가_아닌_도메인_상태_revoke_at이_기록된다() -> None:
    """전이≠기록: revoke는 도메인 admission 상태 전이(revoked_at 붙음)."""
    store = make_store()
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)

    revoked = store.revoke(tok.token_id)

    assert revoked is not None
    assert revoked.revoked_at is not None
    # 처음 발급 때는 revoked_at 없음
    assert tok.revoked_at is None


def test_backup_role_token_발급과_검증() -> None:
    """role=backup 토큰도 같은 로직으로 발급·검증."""
    store = make_store(["backup_tok"])
    raw, tok = store.issue(OWNER_ID, "backup", now=T0)

    assert tok.role == "backup"
    verified = store.verify(raw, now=T0)
    assert verified is not None
    assert verified.role == "backup"


def test_여러_owner_의_token이_독립적으로_관리된다() -> None:
    """owner 격리: owner_alice 토큰 revoke가 owner_bob 토큰에 영향 없음."""
    tokens = ["tok_alice", "tok_bob"]
    idx = 0

    def factory() -> str:
        nonlocal idx
        v = tokens[idx]
        idx += 1
        return v

    store = InMemoryTokenStore(token_factory=factory)
    raw_alice, tok_alice = store.issue("owner_alice", ROLE, now=T0)
    raw_bob, _ = store.issue("owner_bob", ROLE, now=T0)

    store.revoke(tok_alice.token_id)

    assert store.verify(raw_alice, now=T0) is None
    assert store.verify(raw_bob, now=T0) is not None
