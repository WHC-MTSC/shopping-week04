# 쇼핑 주문처리 작업량 예측 · 4주차

`data/model-input.csv`는 사용자의 2주차 `shopping-week02/result/model-input.csv`를 그대로 복사한 자료입니다. 원본은 UCI Machine Learning Repository의 [Online Retail](https://archive.ics.uci.edu/dataset/352/online+retail)입니다. 한 행은 연속된 한 시간이며, 목표 `positive_sales_invoice_count`는 그 시간의 양수 판매전표 건수입니다. 실제 주문 수나 서버 부하를 직접 측정한 값은 아닙니다.

## 특징과 목표

- **목표:** `positive_sales_invoice_count` (다음 한 시간의 건수)
- **예측 시점:** 목표 시간이 시작할 때. 직전 시간이 끝나서 그 건수는 기록되었다고 가정합니다.
- **최종 특징:** 시각, 요일, 월, 주말 여부, 1시간 전 건수, 24시간 전 건수.
- **제외:** 목표 시간의 실제 건수와 원본의 전표·고객·상품 식별자 및 실제 매출.

## 실행

Python 3.12 환경에서 확인했습니다.

```sh
python -m pip install -r requirements.txt
python train.py --data data/model-input.csv --out artifacts
```

`train.py`는 시간순 60% 학습, 20% 검증, 20% 최종 시험을 사용합니다. 검증 평균 절대오차(MAE)로 특징 조합을 고른 뒤 학습·검증 구간으로 다시 학습하고, 마지막 시험 구간을 한 번 평가합니다. 결과는 `artifacts/metrics.json`, `artifacts/test-predictions.csv`, `artifacts/model.joblib`에 저장됩니다. `joblib` 모델 파일은 이 저장소에서 직접 생성한 신뢰 가능한 파일만 불러오세요.

새 입력 CSV에는 최종 특징 6개 열이 필요합니다. 한 시간의 건수를 예측하려면 다음처럼 실행합니다.

```sh
python predict.py --input new-hours.csv --output predicted-hours.csv
```

## 결과와 해석

숫자와 판단 근거는 [4주차 결과 보고서](WEEK04_REPORT.md) 및 [기계 판독용 지표](artifacts/metrics.json)에 있습니다. 시험 구간의 ±2건 일치율은 **82.37%**, 정확히 같은 건수의 일치율은 **67.41%**, MAE는 **1.1456건/시간**입니다. ±2건 일치율은 허용오차 기준이며 분류 정확도가 아닙니다. 바쁜 시간(실제 10건 이상)의 MAE는 4.2924건/시간이므로 운영 인력 결정을 자동화할 정도로 검증되지는 않았습니다.
