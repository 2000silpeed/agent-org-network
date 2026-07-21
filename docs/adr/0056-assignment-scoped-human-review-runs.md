# ADR 0056 — Assignment-scoped Human Review Run과 다중 reviewer threshold

날짜: 2026-07-20  
상태: Accepted

## 맥락

ADR 0055의 all/any/quorum 사람 requirement는 서로 독립된 reviewer evidence를 요구한다. 그러나 기존 base `ReviewRun`은 `(org_id, requirement_id, run_attempt)`가 유일하여 requirement마다 동시 run 하나만 표현한다. 이를 수정하거나 reclaim attempt를 독립 reviewer evidence로 세면, 같은 reviewer의 재시도가 threshold를 부당하게 충족시킬 수 있다.

## 결정

1. 기존 `ReviewRun`과 S1b.2 lease schema는 변경하지 않는다. v4 additive companion은 `ReviewRequirement -> ReviewerAssignment[*] -> AssignmentReviewRun[*] -> FindingFreeHumanTerminal`로 cardinality를 분리한다.
2. `ReviewerAssignment`는 특정 사람 reviewer, immutable assignment ordinal/digest, contributor·policy·provenance·content/rubric/input digest를 갖는다. 같은 requirement의 같은 reviewer subject는 한 assignment만 가능하며 contributor와 같거나 cycle 안의 다른 human requirement에 재사용되면 등록 write 0이다.
3. `AssignmentReviewRun`은 assignment별 attempt/lease state를 소유한다. reclaim/renew/retry는 같은 assignment의 attempt/epoch만 바꾸며, terminal evidence는 assignment당 최대 하나다.
4. completion policy는 candidate assignment 수 `N`과 required terminal count `K`로 정규화한다: `all`은 `K=N`, `any`는 `K=1`, `quorum`은 `1 <= K <= N`이다. 서로 다른 assignment의 finding-free terminal receipt K개만 requirement를 만족시킨다.
5. v4는 explicit manifest/catalog/cycle mirror와 per-cycle ownership marker를 설치한다. v4-provisioned cycle은 v4 writer만 terminalize하며 v3 writer는 fail-closed한다. legacy cycle을 assignment-level evidence로 backfill하거나 추정하지 않는다.
6. terminal UoW는 authoritative DB-time lease/authority validation, assignment/run CAS, immutable receipt/result/audit/outbox, threshold와 cycle CAS를 한 transaction에서 처리한다. BindingReady/source/proposal/evaluation/promotion은 여전히 write 0이다.

## 결과

- 다중 사람 검토의 독립성과 quorum을 reclaim/retry와 혼동하지 않는다.
- 기존 strict v1/v2/v3/lease/AI-batch capability 계약을 보존한다.
- legacy active cycle은 v4 evidence로 자동 승격되지 않으며, 필요하면 명시적인 새 revision/cycle registration을 요구한다.
