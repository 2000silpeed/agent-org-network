"""실 `fastembed` Embedder 어댑터 — owner측 로컬 ONNX 임베딩 [게이트 밖].

`okf_dedup.Embedder` Protocol을 만족하는 **실 임베딩 어댑터**. `FakeEmbedder`(게이트
기본·고정 벡터 주입)와 *같은 포트*(`embed(texts) -> tuple[tuple[float, ...], ...]`)를
만족한다. owner측에서 near-dup 후보 탐지용 임베딩을 로컬 ONNX(`fastembed`)로 계산한다
(ADR 0032 OQ-4 — `intfloat/multilingual-e5-small`·다국어·torch 없음·중앙 토큰 0).

게이트 경계(`provider_transport_claude_code.py` 패턴 그대로):
  - 이 모듈은 `fastembed`를 **모듈 상단에서 import하지 않는다** — `dedup` extra 미설치
    환경에서 이 모듈을 import해도 코어가 안 깨지게(클래스 생성 시점에 지연 import·명확한
    에러). 게이트(`uv run pytest`)는 이 어댑터를 *호출하지 않는다*(결정론 스위트는
    `FakeEmbedder`·라우트는 `select_embedder` 모킹). 게이트는 import·타입만 통과한다.
  - 실 동작 검증은 수동 시연(`AON_EMBEDDER=fastembed` + 한국어 텍스트 cosine 실측)이다.

e5 규약(이 어댑터 내부 책임 — 포트 계약은 모른다):
  - **prefix**: e5 계열은 입력에 `"query: "`/`"passage: "` prefix를 붙여야 제 성능이 난다.
    dedup은 *대칭 비교*(new·existing 둘 다 개념 본문)라 양쪽 모두 `"query: "`를 붙인다
    (ADR 0032 OQ-4 근거 절·OQ-5 임계가 이 prefix 전제로 정해졌다).
  - **풀링·정규화**: MEAN pooling + L2 normalization을 fastembed(`add_custom_model`의
    `pooling=MEAN, normalization=True`)가 적용한다 — 출력 벡터 norm=1.0(실측). 포트가
    "벡터는 L2 정규화돼 있다"고 가정하므로(ADR 0032 §C) dot product가 곧 cosine이다.

불변식:
  - **포트 무변경** — `embed(texts: Sequence[str]) -> tuple[tuple[float, ...], ...]`
    (`okf_dedup.Embedder` Protocol 그대로). numpy 타입을 포트에 노출하지 않는다(어댑터가
    numpy→tuple 변환). 빈 입력은 빈 튜플.
  - **중앙 토큰 0** — owner 기기 로컬 ONNX 추론. 네트워크는 첫 모델 다운로드(HF)뿐이고
    추론은 로컬·키 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastembed import TextEmbedding

# ADR 0032 OQ-4 결정 모델 — 다국어 ONNX·한국어 포함·torch 없음·dim=384.
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-small"
_EMBED_DIM = 384

# e5 대칭 비교 prefix(ADR 0032 OQ-4 근거 절) — new·existing 양쪽 동일하게 붙인다.
_E5_QUERY_PREFIX = "query: "

# 모델별 임베딩 prefix 정책 — 계열마다 규율이 다르다:
#   - e5 계열(intfloat/multilingual-e5-*): 입력에 "query: "/"passage: " prefix를 붙여야
#     제 성능이 난다. dedup은 대칭 비교라 양쪽 모두 "query: "(ADR 0032 OQ-4).
#   - bge 계열(BAAI/bge-*): 문서 임베딩엔 instruction/prefix 불요(대칭 유사도 그대로).
#   - 미지 모델: prefix 없음(보수 기본) — 잘못된 prefix가 성능을 떨어뜨릴 수 있어, 계열을
#     모르면 아무것도 붙이지 않는다(`prefix_for_model` docstring 참조).
# 접두(prefix) 매칭 — 정확 모델명이 아니라 계열 접두로 판정(향후 e5/bge 변종 자동 포괄).
_PREFIX_POLICY_BY_FAMILY: tuple[tuple[str, str], ...] = (
    ("intfloat/multilingual-e5", _E5_QUERY_PREFIX),
    ("intfloat/e5", _E5_QUERY_PREFIX),
    ("BAAI/bge", ""),
)


def prefix_for_model(model_name: str) -> str:
    """모델명 → 임베딩 입력 prefix(계열별 정책·미지 모델은 "" 보수 기본).

    e5 계열은 `"query: "`, bge 계열은 없음(`""`)을 반환한다. 어느 알려진 계열에도
    안 맞으면 빈 문자열을 낸다 — 계열을 모르는 채 e5식 prefix를 붙이면 오히려 성능이
    나빠질 수 있어, 보수적으로 아무 prefix도 붙이지 않는 게 안전 기본이다.
    매칭은 계열 접두(예: `intfloat/multilingual-e5`)로 판정해 같은 계열 변종을 자동 포괄한다.
    """
    for family_prefix, embed_prefix in _PREFIX_POLICY_BY_FAMILY:
        if model_name.startswith(family_prefix):
            return embed_prefix
    return ""


def _register_e5_small(text_embedding_cls: type[TextEmbedding]) -> None:
    """`intfloat/multilingual-e5-small`을 fastembed 커스텀 모델로 등록(멱등).

    fastembed 0.8 기본 레지스트리에는 e5-large만 있어 e5-small을 `add_custom_model`로
    등록한다(HF `intfloat/multilingual-e5-small` 원본 ONNX·`onnx/model.onnx`). 이미 등록돼
    있으면(ValueError) 무시한다 — 같은 프로세스에서 두 번째 인스턴스 생성 시 재등록을 피한다.
    pooling=MEAN·normalization=True가 e5 규약(평균 풀링·L2 정규화)을 fastembed 내부에서 적용.
    """
    from fastembed.common.model_description import ModelSource, PoolingType

    try:
        text_embedding_cls.add_custom_model(
            model=DEFAULT_EMBED_MODEL,
            pooling=PoolingType.MEAN,
            normalization=True,
            sources=ModelSource(hf=DEFAULT_EMBED_MODEL),
            dim=_EMBED_DIM,
            model_file="onnx/model.onnx",
        )
    except ValueError:
        # 이미 등록됨(레지스트리 중복) — 멱등 무시.
        pass


def _supported_model_names(text_embedding_cls: type[TextEmbedding]) -> list[str]:
    """fastembed 지원 모델명 목록(에러 메시지용·조회 실패 시 빈 리스트)."""
    try:
        raw = text_embedding_cls.list_supported_models()
    except Exception:
        return []
    names: list[str] = []
    for m in raw:
        name = m.get("model") if hasattr(m, "get") else getattr(m, "model", None)
        if isinstance(name, str):
            names.append(name)
    return names


class FastEmbedEmbedder:
    """실 fastembed Embedder — owner측 로컬 ONNX 임베딩(게이트 밖·ADR 0032 OQ-4).

    `okf_dedup.Embedder` Protocol을 만족(`FakeEmbedder`와 교체 가능). 생성 시점에
    `fastembed`를 지연 import해 모델을 로드한다(extra 미설치면 명확한 에러). `embed`는
    텍스트마다 **모델별 prefix 정책**(`prefix_for_model`)에 따라 prefix를 붙여 인코딩하고
    numpy 벡터를 float tuple로 변환해 반환한다 — prefix·풀링·정규화는 전부 이 어댑터
    내부 책임이고 포트엔 안 샌다.

    model_name(기본 `intfloat/multilingual-e5-small`·ADR 0032 OQ-4)으로 임베딩 모델을
    고른다. e5 계열은 `"query: "` prefix, bge 계열은 없음, 미지 모델은 없음(보수 기본).
    fastembed가 모르는 모델명이면 생성 시 명확한 ValueError로 감싼다.
    """

    def __init__(self, model_name: str = DEFAULT_EMBED_MODEL) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # `dedup` extra 미설치
            raise SystemExit(
                "AON_EMBEDDER=fastembed 인데 fastembed가 없습니다 — dedup extra를 설치하세요: "
                "pip install 'agent-org-network[dedup]'  (uv: uv sync --extra dedup)"
            ) from exc
        self._model_name = model_name
        self._prefix = prefix_for_model(model_name)
        if model_name == DEFAULT_EMBED_MODEL:
            _register_e5_small(TextEmbedding)
        try:
            self._model: TextEmbedding = TextEmbedding(model_name=model_name)
        except Exception as exc:  # fastembed 미지원 모델명 등 — 명확한 ValueError로 감싼다.
            supported = _supported_model_names(TextEmbedding)
            raise ValueError(
                f"fastembed가 임베딩 모델 {model_name!r}을(를) 로드하지 못했습니다: {exc}. "
                f"지원 모델은 TextEmbedding.list_supported_models()로 확인하세요"
                + (f" (예: {', '.join(supported[:5])})" if supported else "")
                + "."
            ) from exc

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """texts → 임베딩 벡터(L2 정규화·float tuple). 빈 입력은 빈 튜플.

        각 텍스트에 모델별 prefix(`prefix_for_model` — e5는 `"query: "`, bge/미지는 없음)를
        붙여 대칭 비교(ADR 0032 OQ-4)·MEAN 풀링·L2 정규화는 fastembed 내부. numpy float64
        벡터를 포트 계약(float tuple)으로 변환한다.
        """
        text_list = list(texts)
        if not text_list:
            return ()
        prefixed = [f"{self._prefix}{t}" for t in text_list]
        vectors: list[tuple[float, ...]] = []
        raw: Any
        for raw in self._model.embed(prefixed):
            vectors.append(tuple(float(x) for x in raw))
        return tuple(vectors)
