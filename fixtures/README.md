# design_critique 캘리브레이션 픽스처

`design_critique` 그래프의 A/B 판정을 재는 고정 입력과 정답이다. GRAPH-54에서
현재 규모(통제 8쌍 + 실제 1쌍)로 마무리했고, 그 이상은 실제 자료가 확보될 때 늘린다.

## 구성

| 파일 | 내용 |
| :-- | :-- |
| `schema.json` | 픽스처 스키마. `asset_type`은 `logo`·`landing`·`print` |
| `ctrl_logo_01~08.json` | 통제 쌍. 같은 브리프에서 한 속성만 바꿔 특정 축만 흔든다 |
| `real_print_01.json` | 실제 쌍(Class101 표지 예제 완성 vs 응용). `gold: null` |

## gold 규칙

- `gold`는 **사람이 확정한 판정**이다. 모델이 낸 판정을 gold로 쓰지 않는다.
- 아직 판정하지 않은 픽스처는 `gold: null`로 둔다. 측정 스크립트가 건너뛴다.
- 분포를 균형 있게 유지한다. 현재 4 A / 3 B / 1 BOTH_REJECT. 한쪽으로 몰리면
  `baseline_agreement`(가장 흔한 gold를 항상 찍는 기준선)가 올라가 지표가 무의미해진다.
- 통제 쌍이라도 gold를 결과에 맞춰 고치지 않는다. 고칠 근거는 설계 의도뿐이다.

## 측정 절차

```bash
python3 scripts/calibrate_critique.py --validate-only
python3 scripts/calibrate_critique.py --results <결과.json 또는 디렉터리>
```

1. 설정을 바꾸면 **같은 설정으로 3회** 실행한다. 1회 값은 쓰지 않는다.
2. 보고는 중앙값과 범위로 한다. 8쌍에서 1쌍은 12.5%p라 단일 회차 차이는 노이즈와 구분되지 않는다.
3. `human_agreement`는 반드시 `baseline_agreement`와 같이 본다. 기준선보다 높지 않으면 판정이 내용이 아니라 자리(A/B)를 따라간 것이다.
4. 4지표: `human_agreement`, `baseline_agreement`, `halo_rate`, `swap_reversal_rate`, `abstain_rate`.
5. 축 단위 스왑 불일치는 결과 JSON의 `axis_swap_flips`로 따로 센다. 페어 지표보다 민감하다.

## 측정한 것 (2026-11-27, GRAPH-54)

- 통제 8쌍 3회: agreement 75.0 / 87.5 / 75.0%, abstain 25.0 / 12.5 / 25.0%, 축 불일치 28/168(16.7%)
- 반복 실행 변동은 축 56개 중 5개(8.9%). 남은 흔들림은 무작위가 아니라 A/B 자리 민감도다.
- `distinctiveness`가 가장 불안정(축 불일치 9/24). 루브릭 문구를 조여도 9/24에서 움직이지 않았다(PR #120 → #121 되돌림).

## 하지 않는 것

- 8쌍으로 루브릭 문구를 미세 조정하지 않는다. 판별력이 없어 개선과 착시를 구분할 수 없다.
- 실제 쌍을 강의 자료에서 억지로 만들지 않는다. 3,591파일 중 "같은 과제·다른 해법"은 4~5건뿐이다.
