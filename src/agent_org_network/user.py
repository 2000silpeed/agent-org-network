from pydantic import BaseModel


class User(BaseModel, frozen=True):
    id: str
    manager: str | None = None
    # SSO/OIDC 신원 매핑 키(T7.1·ADR 0021 결정 3) — verified email → 이 User.
    # 하위호환 기본 None(기존 User 생성·테스트 무영향). admission 무관(User는 카드가
    # 아니라 Registry.validate가 검증하지 않음). None이면 SSO 매핑 대상에서 빠진다(0매칭 거부).
    email: str | None = None
