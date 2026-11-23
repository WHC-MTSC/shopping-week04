# 서비스 정의 점검표

검토 기준일: 2026-11-16. README의 서비스 정의를 API·입력 검증·사용자 화면·로그 계약과 대조했습니다. 아래의 구현 일치는 실제 점포의 업무 효과나 신규 팀원의 배포 성공을 뜻하지 않습니다.

**한 문장 정의: 쇼핑몰 운영 담당자가 매 정각, 직전 168시간의 양수 판매 거래에 해당하는 실제 전표 건수(거래 없는 시간은 0건)를 입력해 다음 한 시간의 작업량 단계(없음·보통·혼잡)를 예측하고 업무 준비에 참고하는 로컬 검증용 서비스입니다.**

| 점검 항목 | 확정된 정의 | 구현·문서 근거 | 판정 및 남은 확인 |
|---|---|---|---|
| 누가 사용하는가 | 시간별 판매 집계를 확인하고 업무 순서를 정하는 쇼핑몰 운영 담당자 | [README](../README.md), [API metadata](../workload/api.py), [화면](../workload/static/index.html) | 설명 일치. 실제 운영 담당자의 사용성·효과 검증은 별도 필요 |
| 언제·왜 사용하는가 | 정각에 직전 시간 집계가 완료된 뒤 다음 한 시간의 대기 업무·준비 상태를 점검 | [API prediction_time](../workload/api.py), [입력 시간 검증](../workload/schemas.py) | 직전 집계가 제때 완료된다는 운영 가정. 자동 집계 연동 기능은 없음 |
| 입력 | 목표 정각과 바로 이전 168시간의 연속된 시간별 실제 판매전표 건수 | [ForecastRequest/Observation](../workload/schemas.py), [샘플](../examples/request.json) | 정확히 168행, 시간 순서·정각·중복/누락, 정수 0~100,000 검증. 0건 시간도 필요 |
| 시간·집계 기준 | 원본 2010–2011년 영국 소매 데이터의 현지 시각과 `positive_sales_invoice_count` 집계 | [원본 모델 입력](../data/model-input.csv), [데이터 검증](../workload/dataset.py), [화면 주의문](../workload/static/index.html) | 시간대 없는 입력을 사용하며 한국 시각/UTC로 자동 변환하지 않음. 다른 점포는 기준 정합성과 새 데이터 검증 필요 |
| 예측 대상 | 목표 정각부터 다음 한 시간의 판매전표 건수를 대리 지표로 삼은 작업량 단계 | [단계 정의](../workload/artifact.py), [피처·라벨](../workload/features.py) | 없음 `idle`=0건, 보통 `normal`=1~9건, 혼잡 `busy`=10건 이상. 직원 수·처리 시간의 실측 기준은 아님 |
| 출력 | 목표 시각, 단계, 건수 구간, idle/normal/busy 순서의 확률, 모델 버전 및 저장 식별자·상태 | [ForecastResponse](../workload/schemas.py), [RecordedForecast](../workload/log_models.py), [예측 API](../workload/api.py) | 문서·API·화면 일치. 확률은 보정되지 않은 모델 출력이며 정답 보장 수준이 아님 |
| 하지 않는 예측 | 정확한 주문 건수·상품 수량·실제 처리 시간·필요 인원, 예측값을 누적 입력하는 여러 시간 앞 예측 | [README의 범위](../README.md), [API 출력](../workload/schemas.py) | 위 결과를 제공한다고 설명하지 않음. 실제 인력 배치 자동 결정 기능 없음 |
| 예측 모델 | 저장된 기존 모델로 입력 피처를 계산하고 단계 확률을 산출 | [모델 서빙](../workload/artifact.py), [공통 피처](../workload/features.py), [모델 검증기](../workload/model_check.py) | 과거 평가 및 서빙 일치 증거는 있음. 새 점포·현재 시점 성능은 미검증 |
| 사용자 정답 | 실제 건수, 실제 단계, 예측 정오(T/F) 중 사용자가 확인 가능한 값 | [FeedbackRequest](../workload/log_models.py), [정답 API](../workload/log_routes.py) | 정답을 예측 ID에 연결하고 수정 revision 보존. 정답을 모르면 입력하지 않음. 사용자 제출값은 외부 감사된 사실이 아님 |
| 입력 출처 | 화면 기본값은 실습·테스트 `demo`; 확인한 실제 집계는 `user`를 명시 선택 | [출처 선택 화면](../workload/static/index.html), [입력 처리](../workload/static/app.mjs) | CSV·직접 편집은 선택 유지, 샘플 버튼은 demo로 설정. 출처 선택은 사용자 진술이며 실제 데이터 인증 기능은 아님 |
| 수집·조회 | 입력·예측·정답을 SQLite에 먼저 저장하고 선택 시 S3 또는 CloudWatch로 재시도 전송 | [저장소](../workload/log_store.py), [전송](../workload/log_delivery.py), [CloudWatch](../workload/cloudwatch_delivery.py), [AWS 안내](AWS_SETUP.md) | local, local-s3 모사, 실제 S3, CloudWatch를 구별. 로컬 로그 화면이나 수락 표시만으로 원격 읽기 검증을 대체하지 않음 |
| 재학습 | 명시적 실제 건수·단계는 지도 데이터, T/F는 보상 데이터로 전처리하고 후보를 시간순 평가 | [전처리](TRAINING_DATA.md), [실습 기록](REVIEW_ITERATION.md) | demo는 기본 제외. 과거 재생 후보는 실습용이며 운영 승격 불가. 실습 성능을 실제 운영 개선으로 표시하지 않음 |
| 배포·접근 범위 | Windows PC의 `127.0.0.1`에서 HTML/API를 함께 실행하는 프로토타입 | [배포 스크립트](../scripts/deploy-local.ps1), [배포 안내](DEPLOYMENT.md) | 각 팀원 PC에 따로 설치. 공개 서비스·원격 공유·사용자 인증·다중 사용자 권한 관리의 검증 범위가 아님 |
| 신규 수령자 재현 | 문서만 보고 자신의 PC에서 배포·예측·정답 저장·재시작 후 조회 | [첫 실행 안내 및 제출 서식](GETTING_STARTED.md) | **팀원 직접 실행은 미완료.** 사람별 실행 결과와 증거를 받은 뒤 별도로 판정 |

## 오해를 방지할 표현

- 화면의 “혼잡”은 해당 시간 판매전표 10건 이상이라는 모델 라벨입니다. 실제 현장의 처리 역량이나 직원 수를 직접 측정하지 않습니다.
- 과거 샘플과 자동 재생은 `data_source=demo`이며 실제 팀원 행동이나 새 운영 관측이 아닙니다. 실제 데이터의 정답을 모를 때 임의 값을 채우지 않습니다.
- CloudWatch 스타일 로컬 화면, `simulated`, `cloudwatch_sent`, 원격 JSON 대조 성공은 서로 다른 확인 단계입니다. [검증 기록](REVIEW_ITERATION.md)에 어떤 단계를 수행했는지 구분했습니다.
- Python·JavaScript 검사나 작성자 PC 배포가 통과해도 다른 팀원의 독립 배포가 완료된 것은 아닙니다. 팀원별 증거는 [제출 서식](GETTING_STARTED.md#7-팀원-직접-실행-결과-제출)에 추가합니다.

현재 정의와 구현의 핵심 입력·출력·대상 사용자·로컬 운영 범위는 일치합니다. 남은 사람 중심 검증은 신규 팀원의 독립 실행과 실제 운영 담당자의 사용성 확인이며, 새 점포 적용에는 별도 데이터·성능 검증이 필요합니다.
