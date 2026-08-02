import pytest

from agent_org_network.owner_pairing_digest import (
    OwnerPairingDigestUnavailable,
    owner_pairing_redeem_request_digest,
)


def test_redeem_digest는_secret없는공개projection에만결박된다() -> None:
    first = owner_pairing_redeem_request_digest(
        intent_id="intent-1",
        idempotency_key="redeem-1",
        device_key_thumbprint="A" * 43,
    )
    same = owner_pairing_redeem_request_digest(
        intent_id="intent-1",
        idempotency_key="redeem-1",
        device_key_thumbprint="A" * 43,
    )
    changed_key = owner_pairing_redeem_request_digest(
        intent_id="intent-1",
        idempotency_key="redeem-2",
        device_key_thumbprint="A" * 43,
    )
    assert first == same
    assert first != changed_key
    assert len(first) == 64


@pytest.mark.parametrize(
    "kwargs",
    [
        {"intent_id": "../escape", "idempotency_key": "redeem-1", "device_key_thumbprint": "A" * 43},
        {"intent_id": "intent-1", "idempotency_key": "redeem-1", "device_key_thumbprint": "short"},
    ],
)
def test_redeem_digest는잘못된공개projection을거부한다(kwargs: dict[str, str]) -> None:
    with pytest.raises(OwnerPairingDigestUnavailable):
        owner_pairing_redeem_request_digest(**kwargs)
