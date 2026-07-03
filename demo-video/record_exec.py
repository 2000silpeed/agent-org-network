"""임원 시연용 실 화면 3클립 녹화 — 깨끗한 원본(자막·타이틀 미번인, 합성은 HyperFrames).

클립 A: 프런트 /ask 채팅 — 질문 타이핑→즉답(실 도메인 로직·실 claude 사전 산출 답).
클립 B: 콘솔 SSE 관전(/console/view) — 질문이 흘러들어오는 실시간 이벤트.
클립 C: owner 검토면(HITL) — 사전 스테이징된 실 초안을 검토·수정·전송(실 claude 초안).

전제: serve_demo(8765)·frontend(3000)·클립C 스택(중앙 8020+워커 8792)은 호출측이 준비.
"""

import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
REC = HERE / "recordings-exec"
REC.mkdir(exist_ok=True)

VP = {"width": 1920, "height": 1080}


def type_slow(page, selector: str, text: str, delay_ms: int = 45) -> None:
    page.click(selector)
    page.type(selector, text, delay=delay_ms)


def save(page, ctx, name: str) -> None:
    path = page.video.path()
    ctx.close()
    dst = REC / f"{name}.webm"
    shutil.move(path, dst)
    print(f"[saved] {name} -> {dst}")


def clip_a_ask(p) -> None:
    """프런트 채팅: 질문 → 라우팅 → 근거 계산 답."""
    ctx = p.chromium.launch().new_context(
        viewport=VP, record_video_dir=str(REC), record_video_size=VP
    )
    page = ctx.new_page()
    page.goto("http://127.0.0.1:3000/ask")
    page.wait_for_selector("#ask-input")
    page.wait_for_timeout(1800)
    type_slow(
        page,
        "#ask-input",
        "단순 변심인데 결제한 지 20일 됐어요. 10만 원 결제했는데 환불 얼마나 받을 수 있나요?",
        delay_ms=38,
    )
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    # 즉답이지만 렌더/스트림 여유
    page.wait_for_timeout(2500)
    try:
        page.get_by_text("45,000원").first.wait_for(timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(5200)  # 답 읽을 시간(내레이션 자리)
    save(page, ctx, "clip-a-ask")


def clip_b_console(p) -> None:
    """콘솔 관전: 로그인 → /console/view → 질문 3발이 실시간으로 흐름."""
    ctx = p.chromium.launch().new_context(
        viewport=VP, record_video_dir=str(REC), record_video_size=VP
    )
    page = ctx.new_page()
    # 운영자 로그인(무비밀번호 데모) — 같은 컨텍스트 쿠키로 /console/feed 인증 통과
    r = page.request.post("http://127.0.0.1:8765/login", data={"user_id": "cs_lead"})
    print("[b] login:", r.status)
    page.goto("http://127.0.0.1:8765/console/view")
    page.wait_for_timeout(2500)
    for q in [
        "환불은 어떻게 받을 수 있나요?",
        "직원 평가는 어떻게 진행되나요?",
        "계약 조건을 바꿀 수 있나요?",
    ]:
        page.request.post("http://127.0.0.1:8765/ask", data={"question": q})
        page.wait_for_timeout(2600)
    page.wait_for_timeout(3200)
    save(page, ctx, "clip-b-console")


def clip_c_hitl(p) -> None:
    """owner 검토면: 보류 초안 확인 → 문구 추가 → 수정 전송(사전 스테이징 전제)."""
    ctx = p.chromium.launch().new_context(
        viewport=VP, record_video_dir=str(REC), record_video_size=VP
    )
    page = ctx.new_page()
    page.goto("http://127.0.0.1:8792/")
    page.wait_for_selector("textarea.draft-text", timeout=15000)
    page.wait_for_timeout(3800)  # 초안을 읽는 호흡
    # 본문 끝으로 이동해 담당자 확인 문구 추가(mac: Meta+ArrowDown = 문서 끝)
    ta = page.locator("textarea.draft-text").first
    ta.click()
    page.keyboard.press("Meta+ArrowDown")
    page.wait_for_timeout(600)
    page.keyboard.type("\n\n[담당자 확인: 이번 달 카드사 정산 지연 없음 — 안내 그대로 진행]", delay=40)
    page.wait_for_timeout(1600)
    page.get_by_text("수정 전송").first.click()
    page.wait_for_timeout(5200)
    save(page, ctx, "clip-c-hitl")


def clip_d_precedent(p) -> None:
    """다툼→합의→판례→자동 라우팅 아크(핵심 학습 루프·serve_demo·프런트)."""
    ctx = p.chromium.launch().new_context(
        viewport=VP, record_video_dir=str(REC), record_video_size=VP
    )
    page = ctx.new_page()
    # 1) 공동 도메인 질문 → 다툼(Contested)
    page.goto("http://127.0.0.1:3000/ask")
    page.wait_for_selector("#ask-input")
    page.wait_for_timeout(1400)
    type_slow(page, "#ask-input", "보상 문제는 누가 처리하나요?", delay_ms=34)
    page.keyboard.press("Enter")
    page.wait_for_timeout(3600)  # "담당을 확인하고 있어요" 노출
    # 2) 처리함 — 신원 선택(cs_lead) → 담당 지정(합의) → 판례 저장
    page.goto("http://127.0.0.1:3000/inbox")
    page.wait_for_timeout(2200)
    page.locator("button, [role=button], .cursor-pointer", has_text="cs_lead").first.click()
    page.wait_for_timeout(2400)
    page.get_by_text("cs_ops 지정").first.click()
    page.wait_for_timeout(2600)  # "내 표 기록됨 — 나머지 대기" 배너
    # 신원 전환: finance_lead도 같은 담당을 지정 → 합의 성립(판례 저장)
    page.locator("button[aria-haspopup='menu']:visible").first.click()
    page.wait_for_timeout(900)
    page.locator("[role='menuitemradio']", has_text="finance_lead").first.click()
    page.wait_for_timeout(1200)
    page.reload()  # 신원 전환 반영(패널 재조회)
    page.wait_for_timeout(2200)
    page.get_by_text("cs_ops 지정").first.click()
    page.wait_for_timeout(3000)  # agreed 배너
    # 3) 같은 질문 재시도 → 판례로 즉시 자동 라우팅
    page.goto("http://127.0.0.1:3000/ask")
    page.wait_for_selector("#ask-input")
    page.wait_for_timeout(900)
    type_slow(page, "#ask-input", "보상 문제는 누가 처리하나요?", delay_ms=30)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4200)  # 즉시 답 스트림
    page.wait_for_timeout(2600)
    save(page, ctx, "clip-d-precedent")


def clip_e_safety(p) -> None:
    """안전 케이스: 권한 밖 거절(급여이체) + 담당 없음 이관(미아)."""
    ctx = p.chromium.launch().new_context(
        viewport=VP, record_video_dir=str(REC), record_video_size=VP
    )
    page = ctx.new_page()
    page.goto("http://127.0.0.1:3000/ask")
    page.wait_for_selector("#ask-input")
    page.wait_for_timeout(1200)
    type_slow(page, "#ask-input", "급여이체 좀 처리해 줘.", delay_ms=34)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4200)
    type_slow(page, "#ask-input", "구내식당 오늘 메뉴 뭐야?", delay_ms=34)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4600)
    save(page, ctx, "clip-e-safety")


def main() -> None:
    which = set(sys.argv[1:]) or {"a", "b", "c"}
    with sync_playwright() as p:
        if "a" in which:
            clip_a_ask(p)
        if "b" in which:
            clip_b_console(p)
        if "c" in which:
            clip_c_hitl(p)
        if "d" in which:
            clip_d_precedent(p)
        if "e" in which:
            clip_e_safety(p)


if __name__ == "__main__":
    main()
