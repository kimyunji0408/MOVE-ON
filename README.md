# MOVE:ON

AI 기반 기후재난 대응 교통약자 이동단절 예측 및 맞춤 대체이동 서비스

## MVP scope

보행 -> 지하철 -> 보행 구조의 frozen demo scenario를 검증한다.

## Frozen demo scenario

은평구청 -> 녹번역 3호선 -> 불광역 3호선 -> 불광제1동주민센터

## Validated route

first-mile:
- 은평구청 -> 녹번역 3호선
- 462.059m
- historical flood overlap 0.0m

subway:
- 녹번 -> 불광
- 3호선
- transfer 0
- totalDstc 1100
- totalReqHr raw 90

last-mile baseline:
- 불광역 3호선 -> 불광제1동주민센터
- 331.712m
- historical flood overlap 9.985m

last-mile alternative:
- 333.743m
- +2.031m
- historical flood overlap 0.0m
- Hausdorff 29.228m reference, current regenerated value may differ within small geometry tolerance

## Important interpretation

- flood trace != flood probability
- historical flood avoidance != guaranteed safety
- elevator/lift = facility existence only
- real-time operation not validated
- OSM accessibility tags are not used for safety judgment
- slope is not used for safety judgment
- totalReqHr unit is not verified

## Data

- OSM walk graph: `data/raw/osm/eunpyeong_walk.graphml`
- Flood traces: `data/processed/flood/seoul_flood_trace_2022_2025.geojson`
- Accessibility: `data/processed/accessibility/candidate_station_accessibility.csv`
- Subway route cache: `data/raw/od_candidates/route_api_cache.json`
- Frozen validation: `data/processed/final_mvp/end_to_end_validation/`
- Frozen scenario: `data/processed/final_mvp/final_mvp_scenario.json`

## Scripts structure

- `scripts/run_pipeline.ps1`: final frozen MVP smoke validation wrapper
- `scripts/validation/`: final scenario validation scripts
- `scripts/diagnostics/`: API and source-data check scripts
- `scripts/archive/`: candidate-search and early validation experiments kept for reproducibility
- `scripts/*.py` and `scripts/*.ps1`: preprocessing and shared pipeline support retained at top level where existing imports rely on them

## Execution

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\run_pipeline.ps1
```

The wrapper validates the frozen scenario and reloads the cleanup report. It does not rerun broad OD search.

## Implemented

- Flood trace preprocessing
- Subway station/accessibility data checks
- Route API cache validation for the frozen subway segment
- Local OSM walking route validation
- Frozen end-to-end MVP scenario validation
- Script inventory and cleanup reports

## Not implemented yet

- historical heavy-rain time-series demo dataset
- rule-based route decision
- Risk Score 또는 동등한 위험상태 판단
- Mobility Failure Point
- Last Accessible Departure
- Alan explanation
- final UI integration
