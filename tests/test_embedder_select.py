"""임베딩 모델 선택 시임 게이트 테스트 — `select_author`·`select_runtime` 대칭.

실 ONNX 로드 없이 결정론으로 검증한다:
  - `FastEmbedEmbedder(model_name=...)` 파라미터 전달(생성자 지연 로드 — fastembed
    `TextEmbedding`을 스텁으로 갈아끼워 실 모델 로드 0).
  - 모델별 prefix 정책 맵(`prefix_for_model`): e5→"query: " / bge→없음 / 미지→없음.
  - `embedder_select.select_embedder`의 `AON_EMBED_MODEL` env 분기·기본값 무변경.
  - fastembed 미지원 모델명 → 명확한 ValueError로 감싼다.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

import agent_org_network.provider_embed_fastembed as pef
from agent_org_network.embedder_select import select_embedder
from agent_org_network.provider_embed_fastembed import (
    DEFAULT_EMBED_MODEL,
    FastEmbedEmbedder,
    prefix_for_model,
)


def _noop_register(_cls: object) -> None:
    """`_register_e5_small` no-op 대체 — 실 fastembed 서브모듈 import를 피한다."""


class _StubTextEmbedding:
    """실 ONNX 대신 model_name만 기록하는 스텁 — 생성자 지연 로드를 실 로드 없이 검증."""

    last_model_name: str | None = None

    def __init__(self, model_name: str) -> None:
        _StubTextEmbedding.last_model_name = model_name
        self.model_name = model_name

    def embed(self, texts: Sequence[str]):  # noqa: ANN201 - 스텁
        for t in texts:
            yield (float(len(t)),)

    @staticmethod
    def list_supported_models() -> list[dict[str, object]]:
        return [{"model": DEFAULT_EMBED_MODEL}]


def _patch_fastembed(monkeypatch: pytest.MonkeyPatch) -> type[_StubTextEmbedding]:
    """`from fastembed import TextEmbedding`이 스텁을 반환하도록 갈아끼운다(실 로드 0)."""
    import sys
    import types

    _StubTextEmbedding.last_model_name = None
    fake_mod = types.ModuleType("fastembed")
    fake_mod.TextEmbedding = _StubTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake_mod)
    # 기본 모델 커스텀 등록(_register_e5_small)은 실 fastembed 서브모듈을 import하므로 no-op로.
    monkeypatch.setattr(pef, "_register_e5_small", _noop_register)
    return _StubTextEmbedding


# --- 1. prefix 정책 맵 ---------------------------------------------------------


def test_e5_계열은_query_prefix() -> None:
    assert prefix_for_model("intfloat/multilingual-e5-small") == "query: "
    assert prefix_for_model("intfloat/multilingual-e5-large") == "query: "


def test_bge_계열은_prefix_없음() -> None:
    assert prefix_for_model("BAAI/bge-m3") == ""
    assert prefix_for_model("BAAI/bge-small-en-v1.5") == ""


def test_미지_모델은_prefix_없음_보수기본() -> None:
    assert prefix_for_model("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") == ""
    assert prefix_for_model("some/unknown-model") == ""


# --- 2. FastEmbedEmbedder 모델 파라미터 전달·prefix 적용 -----------------------


def test_기본_모델_무변경(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_fastembed(monkeypatch)
    emb = FastEmbedEmbedder()
    assert stub.last_model_name == DEFAULT_EMBED_MODEL
    assert emb._model_name == DEFAULT_EMBED_MODEL  # pyright: ignore[reportPrivateUsage]
    assert emb._prefix == "query: "  # pyright: ignore[reportPrivateUsage]


def test_model_name_파라미터가_fastembed로_전달(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_fastembed(monkeypatch)
    emb = FastEmbedEmbedder(model_name="BAAI/bge-m3")
    assert stub.last_model_name == "BAAI/bge-m3"
    assert emb._prefix == ""  # pyright: ignore[reportPrivateUsage]


def test_e5_모델은_embed시_query_prefix를_붙인다(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fastembed(monkeypatch)
    emb = FastEmbedEmbedder(model_name="intfloat/multilingual-e5-large")
    out = emb.embed(["kb"])
    # 스텁 embed는 len(text)를 벡터로 낸다 — "query: kb"=9자 → 9.0.
    assert out == ((9.0,),)


def test_bge_모델은_embed시_prefix_없음(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fastembed(monkeypatch)
    emb = FastEmbedEmbedder(model_name="BAAI/bge-m3")
    out = emb.embed(["kb"])  # prefix 없음 → "kb"=2자 → 2.0.
    assert out == ((2.0,),)


def test_빈_입력은_빈_튜플(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fastembed(monkeypatch)
    emb = FastEmbedEmbedder(model_name="BAAI/bge-m3")
    assert emb.embed([]) == ()


def test_미지원_모델명은_명확한_ValueError(monkeypatch: pytest.MonkeyPatch) -> None:
    """fastembed가 던지는 오류를 명확한 ValueError로 감싼다(모델명 포함)."""

    class _RaisingTextEmbedding(_StubTextEmbedding):
        def __init__(self, model_name: str) -> None:
            raise KeyError(model_name)  # fastembed류 불명확 오류

    import sys
    import types

    fake_mod = types.ModuleType("fastembed")
    fake_mod.TextEmbedding = _RaisingTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake_mod)
    monkeypatch.setattr(pef, "_register_e5_small", _noop_register)

    with pytest.raises(ValueError, match="no/such-model"):
        FastEmbedEmbedder(model_name="no/such-model")


# --- 3. select_embedder env 분기 ----------------------------------------------


def test_select_미설정이면_None(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_EMBEDDER", raising=False)
    assert select_embedder() is None


def test_select_off는_None(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_EMBEDDER", "off")
    assert select_embedder() is None


def test_select_fastembed_기본모델_무변경(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_fastembed(monkeypatch)
    monkeypatch.setenv("AON_EMBEDDER", "fastembed")
    monkeypatch.delenv("AON_EMBED_MODEL", raising=False)
    emb = select_embedder()
    assert isinstance(emb, FastEmbedEmbedder)
    assert stub.last_model_name == DEFAULT_EMBED_MODEL


def test_select_AON_EMBED_MODEL이_모델을_덮는다(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_fastembed(monkeypatch)
    monkeypatch.setenv("AON_EMBEDDER", "fastembed")
    monkeypatch.setenv("AON_EMBED_MODEL", "intfloat/multilingual-e5-large")
    emb = select_embedder()
    assert isinstance(emb, FastEmbedEmbedder)
    assert stub.last_model_name == "intfloat/multilingual-e5-large"
    assert emb._prefix == "query: "  # pyright: ignore[reportPrivateUsage]


def test_select_AON_EMBED_MODEL_빈값이면_기본모델(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_fastembed(monkeypatch)
    monkeypatch.setenv("AON_EMBEDDER", "fastembed")
    monkeypatch.setenv("AON_EMBED_MODEL", "   ")
    select_embedder()
    assert stub.last_model_name == DEFAULT_EMBED_MODEL


def test_select_AON_EMBED_MODEL은_AON_EMBEDDER_미설정이면_무시(monkeypatch: pytest.MonkeyPatch) -> None:
    """AON_EMBED_MODEL만 설정하고 AON_EMBEDDER 미설정 → 비활성(None) 무변경."""
    monkeypatch.delenv("AON_EMBEDDER", raising=False)
    monkeypatch.setenv("AON_EMBED_MODEL", "BAAI/bge-m3")
    assert select_embedder() is None


def test_select_알수없는_값은_SystemExit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_EMBEDDER", "magic")
    with pytest.raises(SystemExit):
        select_embedder()
