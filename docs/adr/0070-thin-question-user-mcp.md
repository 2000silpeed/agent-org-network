# ADR 0070 — Thin Question User MCP

Question User MCP production artifact는 HTTPS 원격 gateway만 사용하는 고정 manifest `{ask_org, get_question}`로 한정한다. feedback·Registry/Card/authoring/worker/admin/approval/manager 도구와 `user_id`·org·role·authority·token 입력은 없다.

pairing은 PKCE loopback을 사용한다. HTTP는 local one-shot `127.0.0.1` callback에만 허용하고 gateway/token endpoint는 HTTPS만 허용한다. raw code/token/verifier는 keychain 밖에 저장·로그·감사하지 않는다. 중앙은 매 요청 verified OIDC identity를 current Registry User와 server-derived principal로 해석하고 central Authority로 create/read-own을 재검증한다. client args/profile/env는 신원·권한 근거가 아니다.

기존 in-process MCP server는 central gateway/test fixture로만 남으며 production aon-mcp artifact가 compose하지 않는다. 중앙 HTTPS gateway와 Registry DB principal resolver, Authority/read-own check는 `central_question_gateway.py`에만 두고, 표준 Central Server production app에는 주입 가능한 `CentralQuestionGatewayRoutes`로 mount한다. 따라서 `question_user_mcp.py`/`aon-mcp` artifact는 FastAPI·central Authority·Registry DB·OIDC·질문 application을 import하지 않는다.
