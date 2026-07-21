# ADR 0051 — 운영 MCP의 공통 권한·감사·사람 통제 계약

- 상태: 제안(Proposed)
- 날짜: 2026-07-15
- 계보: ADR 0050의 중앙 Authority action manifest와 P17.8 운영 웹 권한 경계를 MCP 자동화 표면으로 확장한다.
- 적용 범위: 운영 MCP P0와 카드 관리, 저작 MCP를 위한 별도 AuthoringApplication의 공통 권한·감사·승인 증적 경계, HTTP/MCP 채널 동등성.
- 제외 범위: OIDC/JWKS·production FastAPI(S6), durable policy epoch·원자적 감사·재시작 복구·다중 인스턴스(P17.9), credential MCP와 typed 재admission을 아직 강제할 수 없는 author MCP 도구.

## 맥락

운영자는 웹 UI 없이도 감사, 장애 대응과 자동화를 수행할 수 있어야 한다. 그러나 웹 콘솔의 일부 변경은 성공 감사 기록이 없고, 기존 HITL 토글은 답변 흐름의 승인 설정이지 운영 명령을 승인하는 장치가 아니다. MCP handler가 웹 closure나 저장소를 직접 복제하면 권한·감사·소유권 drift 처리도 채널마다 갈라진다.

## 결정

HTTP와 MCP는 공통 운영 application command/query를 호출한다. P0 MCP 도구는 `monitor.read`, `audit.read`, `org_graph.read`, `session.end`, `hitl.read`, `hitl.write`와 카드 `card.read`, `card.register`, `card.transfer_owner`를 제공한다. 각 도구명은 중앙 action manifest와 정확히 하나로 매핑하며, client 입력으로 조직·실행 주체를 받지 않는다. 카드 등록·이전에서 받는 Owner는 실행 주체가 아니라 대상 리소스 후보이며, 서버 principal과 현재 Registry·승인 증적이 그 변경을 별도로 확인한다.

query는 서버 principal과 현재 authoritative ResourceRef로 인가한 뒤 snapshot을 읽고, DTO를 만들기 직전에 동일 principal과 현재 resource로 sealed grant를 다시 검증한다. mutation은 principal·현재 대상을 확인하고 인가한 뒤 입력을 검증하며, 도메인 서비스 호출 직전에 대상 재조회·재인가한다. 권한·조직·소유권 drift는 write 0이다. 대상 없음과 deny는 중립 not-found 의미로, policy/provider 장애는 unavailable 의미로 수렴한다.

central operational composition은 raw legacy store나 임의 callback을 직접 authority로 소비하지 않는다. Registry/graph, session, audit/monitor, HITL의 각 source instance가 composition-owned `Operational Source Scope Proof`로 configured org와 current provenance/snapshot을 증명해야 한다. proof 부재·instance 교체·revision/digest drift·mixed-org row·source 장애는 다른 조직 행만 걸러 부분 성공시키지 않고 query를 unavailable 또는 대상 비노출로, mutation을 write 0으로 닫는다. `ResourceRef` allow와 source scope proof는 AND 조건이다.

`session.end`, `hitl.write`, `card.register`, `card.transfer_owner`와 structured `author.publish` 변경은 별도 운영 변경 승인 gate의 현재 허용·증적 참조 없이는 중앙 모드에서 실행하지 않는다. evidence는 canonical command SHA-256과 current resource snapshot fingerprint까지 exact 대조한다. 기존 HITL 토글을 이 승인으로 해석하지 않는다. 성공한 변경 뒤에만 append-only procedural audit을 남기며, tool·action·resource·server principal·승인 증적 ID·digest만 기록한다. secret·평문 credential·원문 입력은 기록하지 않는다.

P0의 채널 동등성은 같은 application 권한·명령·감사 경계를 쓴다는 뜻이지, HTTP와 MCP의 응답 형식이 byte 단위로 같다는 뜻은 아니다.

## 결과와 한계

운영 MCP는 임시 빈 매트릭스가 아니라 실제 자동화 표면이 되며, 웹과 다른 우회 권한 경로를 만들지 않는다. 저작 표면은 raw·staged 본문을 범용 운영 application에 넣지 않고 별도 `AuthoringApplication`으로 추출한다. structured publish·edit·delete는 approval·감사와 Git 직전 현재 카드 재admission, commit 뒤 index write 전 재인가를 요구한다. 임의 raw bundle commit과 MCP author run은 현재 카드 기준 typed 재admission을 강제할 수 없어 중앙 MCP에서 열지 않는다. Git 뒤 audit append가 실패하면 변경은 남고 감사가 없을 수 있으며, audit과 mutation의 원자성, durable ordering, exactly-once, crash recovery, 다중 인스턴스 일관성은 아직 보장하지 않는다. 자격 증명은 현 TokenStore에 조직·generation·CAS·worker binding과 durable 승인 증적이 없어 도구를 열지 않으며 S6의 credential authority 선행 과제로 둔다. 실 OIDC/production transport도 후속 단계다.
