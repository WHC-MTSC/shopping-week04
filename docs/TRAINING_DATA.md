# 수집 로그 전처리

CloudWatch/S3 내보내기 도구가 message 포장을 푼 원본 prediction/feedback 이벤트 JSONL을 입력합니다.

```powershell
uv run --locked python -m workload.prepare_logs --input runtime/export/events.jsonl --output runtime/training/prepared
```

`--include-demo`는 체험용 데이터 진단 전용입니다. 이 옵션을 사용한 결과는 summary의 `promotion_eligible=false`이며 운영 모델 승격에 사용하면 안 됩니다. 기본 실행은 `data_source=demo`를 제외합니다.

## 출력 계약

- `supervised.jsonl`: `SupervisedRow` 모델. `target_hour`, `features`(FEATURE_NAMES 순서의 18개 실수), `label`(idle=0, normal=1, busy=2), `prediction_id`, `feedback_id`, `data_source`, `model_version`, `input_fingerprint`.
- `rewards.jsonl`: `RewardRow` 모델. 동일 문맥/식별자와 예측 `action`, 정답 여부 `reward`(1/0), `prediction_event_id`, `feedback_kind`. 에피소드나 다음 상태가 없는 문맥별 행동·보상 기록입니다. 완전한 강화학습 rollout이 아니며, 행동 확률이나 탐색 정책을 기록하지 않아 일반적인 오프폴리시 인과 평가도 보장하지 않습니다.
- `summary.json`: 원본 파일 바이트의 `input_sha256`, demo 포함 여부, 승격 적격 표시, 행 수와 격리 사유. `promotion_eligible=true`는 demo 진단 모드가 아니라는 뜻이며 모델 성능 검증을 대체하지 않습니다.
- `quarantine.jsonl`: 격리 사유와 엄격한 스키마로 파싱된 해당 이벤트. 파싱 불가능한 줄은 원문을 복사하지 않고 줄 번호만 남깁니다. 정형 이벤트도 원래 입력 이력을 포함하므로 수집 로그와 동일한 접근 통제를 적용하세요.

## 정답·검증 규칙

이벤트는 Pydantic `Event`를 strict 모드로 읽습니다. 같은 event_id의 동일 이벤트는 한 번만 사용합니다. 같은 ID의 다른 payload는 명확한 오류로 전체 작업을 중단합니다. prediction_id로 예측과 정답을 연결하고 revision이 가장 큰 수정 정답을 선택합니다. 같은 revision에 서로 다른 이벤트가 있으면 해당 예측의 정답을 격리합니다. 참조 예측 또는 정답이 없는 기록도 격리합니다.

원본 168시간 이력에서 18개 피처를 다시 계산하여 저장 피처와 비교합니다. 확률 범위·합, 최대 확률에 따른 예측 단계(동률이면 서빙과 같은 첫 단계), 목표 시각, 단계·구간 및 사용자 제출 내용과 actual_stage/is_correct의 일관성을 검증합니다. 지도학습 라벨은 실제 건수 또는 실제 단계에서만 만듭니다. T/F 답변은 지도학습에 넣지 않고 보상 데이터에만 넣습니다.

동일 목표 시각과 동일 입력 이력은 한 input_fingerprint로 묶습니다. 여러 예측의 최신 실제 정답이 다른 단계이거나 실제 건수가 서로 다르면 해당 입력을 격리합니다. 실제 단계·건수로 확인되는 예측 정오와 T/F 판정이 충돌하는 경우도 해당 입력을 격리합니다. T/F만 있는 입력에서는 실제 단계를 추론하지 않습니다. 일치하는 지도학습 예시는 하나만 출력합니다. 행은 target_hour 순서로 정렬하며 재학습은 반드시 시간순 분할을 적용해야 합니다. 동일 시각을 학습·검증 세트 양쪽에 나누지 마세요. 수집된 사용자 정답은 외부 감사된 사실이 아니므로 승격 전 별도 검증이 필요합니다.

모듈 API는 `prepare(input_path: Path, output_dir: Path, *, include_demo=False) -> PreparationSummary`입니다. 기존 출력 디렉터리는 덮어쓰지 않으므로 실행마다 새 경로를 지정하세요. 임시 디렉터리에 데이터와 마지막으로 summary를 쓴 뒤 완성된 디렉터리를 최종 경로로 옮깁니다.
