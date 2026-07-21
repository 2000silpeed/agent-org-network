"""P17.5 S4.1 — typed Grounding Knowledge reader와 본문 조립 계약."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest
from pydantic import ValidationError

from agent_org_network.grounding import assemble_grounding_knowledge_text
from agent_org_network.knowledge_store import (
    GroundingKnowledgeFound,
    GroundingKnowledgeInvalid,
    GroundingKnowledgeMissing,
    GroundingKnowledgeReader,
    KnowledgeStore,
    KnowledgeStoreGroundingKnowledgeReader,
)
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc

_SYNCED_AT = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


def _bundle(
    agent_id: str = "primary-card",
    *,
    documents: tuple[KnowledgeDoc, ...] | None = None,
    version: str = "v1",
) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id,
        documents=documents
        if documents is not None
        else (KnowledgeDoc(path="policy/refund.md", body="환불 정책 본문"),),
        version=version,
        synced_at=_SYNCED_AT,
    )


class _StubKnowledgeStore:
    def __init__(self, value: object) -> None:
        self.value = value
        self.requested: list[str] = []

    def get(self, agent_id: str) -> KnowledgeBundleContent | None:
        self.requested.append(agent_id)
        return cast(KnowledgeBundleContent | None, self.value)

    def put(self, content: KnowledgeBundleContent) -> None:
        del content

    def is_stale(self, agent_id: str, *, now: datetime, threshold_s: int) -> bool:
        del agent_id, now, threshold_s
        return False


def _reader(value: object) -> KnowledgeStoreGroundingKnowledgeReader:
    return KnowledgeStoreGroundingKnowledgeReader(_StubKnowledgeStore(value))


def test_grounding_knowledge_result는_strict_frozen_extra_forbid다() -> None:
    found = GroundingKnowledgeFound(agent_id="primary-card", content=_bundle())

    with pytest.raises(ValidationError):
        GroundingKnowledgeMissing.model_validate({"agent_id": 1}, strict=True)
    with pytest.raises(ValidationError):
        GroundingKnowledgeInvalid.model_validate(
            {
                "agent_id": "primary-card",
                "reason_code": "unknown",
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        GroundingKnowledgeFound.model_validate(
            {
                "agent_id": "primary-card",
                "content": _bundle(),
                "unexpected": True,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        found.agent_id = "changed"  # type: ignore[misc]


def test_reader는_store_None을_requested_agent의_Missing으로_매핑한다() -> None:
    assert _reader(None).read("primary-card") == GroundingKnowledgeMissing(agent_id="primary-card")


@pytest.mark.parametrize("agent_id", ["", "bad/id", " space"])
def test_reader는_유효하지_않은_requested_agent_id를_store_호출_전에_거부한다(
    agent_id: str,
) -> None:
    store = _StubKnowledgeStore(None)
    reader = KnowledgeStoreGroundingKnowledgeReader(store)

    with pytest.raises(ValueError):
        reader.read(agent_id)

    assert store.requested == []


def test_reader는_exact_bundle을_canonical_deep_copy_Found로_돌린다() -> None:
    source = _bundle(
        documents=(
            KnowledgeDoc(path="policy/refund.md", body="환불 정책"),
            KnowledgeDoc(path="faq/refund.md", body="환불 FAQ"),
        )
    )

    result = _reader(source).read("primary-card")

    assert type(result) is GroundingKnowledgeFound
    assert result == GroundingKnowledgeFound(agent_id="primary-card", content=source)
    assert result.content is not source
    assert result.content.documents is not source.documents
    assert all(
        copied is not original
        for copied, original in zip(result.content.documents, source.documents, strict=True)
    )
    object.__setattr__(source.documents[0], "body", "변조")
    assert result.content.documents[0].body == "환불 정책"


@pytest.mark.parametrize(
    "raw",
    [
        object(),
        type("KnowledgeBundleContentSubclass", (KnowledgeBundleContent,), {})(
            agent_id="primary-card",
            documents=(KnowledgeDoc(path="policy.md", body="본문"),),
            version="v1",
            synced_at=_SYNCED_AT,
        ),
        KnowledgeBundleContent.model_construct(
            agent_id="primary-card",
            documents=[KnowledgeDoc(path="policy.md", body="본문")],
            version="v1",
            synced_at=_SYNCED_AT,
        ),
        KnowledgeBundleContent.model_construct(
            agent_id="primary-card",
            documents=(KnowledgeDoc(path="policy.md", body="본문"),),
            version=1,
            synced_at=_SYNCED_AT,
        ),
        KnowledgeBundleContent.model_construct(
            agent_id="primary-card",
            documents=(KnowledgeDoc(path="policy.md", body="본문"),),
            version="v1",
            synced_at="not-a-datetime",
        ),
    ],
    ids=["wrong-type", "subclass", "documents-not-tuple", "bad-version", "bad-time"],
)
def test_reader는_bundle_type이나_envelope_위반을_type_mismatch로_낮춘다(
    raw: object,
) -> None:
    assert _reader(raw).read("primary-card") == GroundingKnowledgeInvalid(
        agent_id="primary-card",
        reason_code="type_mismatch",
    )


@pytest.mark.parametrize("raw_agent_id", ["other-card", 17])
def test_reader는_bundle_agent_id_위반을_agent_id_mismatch로_낮춘다(
    raw_agent_id: object,
) -> None:
    raw = KnowledgeBundleContent.model_construct(
        agent_id=raw_agent_id,
        documents=(KnowledgeDoc(path="policy.md", body="본문"),),
        version="v1",
        synced_at=_SYNCED_AT,
    )

    assert _reader(raw).read("primary-card") == GroundingKnowledgeInvalid(
        agent_id="primary-card",
        reason_code="agent_id_mismatch",
    )


def test_reader는_빈_documents를_empty_documents로_낮춘다() -> None:
    assert _reader(_bundle(documents=())).read("primary-card") == GroundingKnowledgeInvalid(
        agent_id="primary-card",
        reason_code="empty_documents",
    )


@pytest.mark.parametrize(
    "documents",
    [
        (object(),),
        (type("KnowledgeDocSubclass", (KnowledgeDoc,), {})(path="policy.md", body="본문"),),
        (KnowledgeDoc.model_construct(path=1, body="본문"),),
        (KnowledgeDoc.model_construct(path=" ", body="본문"),),
        (KnowledgeDoc.model_construct(path="policy.md", body=1),),
        (KnowledgeDoc.model_construct(path="policy.md", body=" "),),
        (
            KnowledgeDoc(path="policy.md", body="첫 본문"),
            KnowledgeDoc(path="policy.md", body="둘째 본문"),
        ),
    ],
    ids=[
        "wrong-doc-type",
        "doc-subclass",
        "path-not-string",
        "blank-path",
        "body-not-string",
        "blank-body",
        "duplicate-path",
    ],
)
def test_reader는_document_위반을_invalid_document로_낮춘다(
    documents: tuple[object, ...],
) -> None:
    raw = KnowledgeBundleContent.model_construct(
        agent_id="primary-card",
        documents=documents,
        version="v1",
        synced_at=_SYNCED_AT,
    )

    assert _reader(raw).read("primary-card") == GroundingKnowledgeInvalid(
        agent_id="primary-card",
        reason_code="invalid_document",
    )


def test_reader는_store_예외를_Missing이나_Invalid로_바꾸지_않는다() -> None:
    error = RuntimeError("temporary storage failure")

    class _RaisingStore(_StubKnowledgeStore):
        def get(self, agent_id: str) -> KnowledgeBundleContent | None:
            del agent_id
            raise error

    reader = KnowledgeStoreGroundingKnowledgeReader(_RaisingStore(None))

    with pytest.raises(RuntimeError) as raised:
        reader.read("primary-card")
    assert raised.value is error


def test_reader의_knowledge_store_identity_seam은_is로만_판정한다() -> None:
    store = _StubKnowledgeStore(None)
    equal_but_distinct = _StubKnowledgeStore(None)
    reader = KnowledgeStoreGroundingKnowledgeReader(store)

    assert reader.matches_knowledge_store(cast(KnowledgeStore, store)) is True
    assert reader.matches_knowledge_store(cast(KnowledgeStore, equal_but_distinct)) is False


def test_grounding_knowledge_reader_Protocol은_read만_요구한다() -> None:
    class _ReadOnlyReader:
        def read(self, agent_id: str) -> GroundingKnowledgeMissing:
            return GroundingKnowledgeMissing(agent_id=agent_id)

    reader: GroundingKnowledgeReader = _ReadOnlyReader()

    assert reader.read("primary-card") == GroundingKnowledgeMissing(agent_id="primary-card")


def test_assemble_grounding_knowledge_text는_primary_supporting과_문서_순서를_보존한다() -> None:
    primary = GroundingKnowledgeFound(
        agent_id="primary-card",
        content=_bundle(
            documents=(
                KnowledgeDoc(path="policy.md", body="정책 본문"),
                KnowledgeDoc(path="faq.md", body="FAQ 본문"),
            )
        ),
    )
    supporting = GroundingKnowledgeFound(
        agent_id="support-card",
        content=_bundle(
            "support-card",
            documents=(KnowledgeDoc(path="legal.md", body="법무 본문"),),
        ),
    )

    assert assemble_grounding_knowledge_text((primary, supporting)) == (
        "### primary-card\n"
        "#### policy.md\n정책 본문\n\n"
        "#### faq.md\nFAQ 본문\n\n"
        "### support-card\n"
        "#### legal.md\n법무 본문"
    )


def test_assemble_grounding_knowledge_text는_primary가_없는_빈_입력을_거부한다() -> None:
    with pytest.raises(ValueError, match="primary"):
        assemble_grounding_knowledge_text(())
