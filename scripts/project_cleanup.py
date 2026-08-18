from __future__ import annotations

import ast
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CLEANUP_DIR = PROJECT_ROOT / "data/processed/project_cleanup"
FINAL_MVP_DIR = PROJECT_ROOT / "data/processed/final_mvp"
E2E_DIR = FINAL_MVP_DIR / "end_to_end_validation"

SCENARIO_JSON = FINAL_MVP_DIR / "final_mvp_scenario.json"
SCRIPT_INVENTORY_JSON = CLEANUP_DIR / "script_inventory.json"
DEPENDENCY_GRAPH_JSON = CLEANUP_DIR / "script_dependency_graph.json"
CLASSIFICATION_JSON = CLEANUP_DIR / "script_classification.json"
CLEANUP_REPORT_JSON = CLEANUP_DIR / "cleanup_report.json"
README = PROJECT_ROOT / "README.md"
RUN_PIPELINE = SCRIPTS_DIR / "run_pipeline.ps1"

BEFORE_SCRIPT_COUNT = 24
MOVED_SCRIPTS = [
    "scripts/validation/validate_final_e2e_mvp.py",
    "scripts/validation/validate_mobility_data.ps1",
    "scripts/diagnostics/check_accessibility_api.py",
    "scripts/diagnostics/check_alan_api.py",
    "scripts/diagnostics/check_hsr_api.py",
    "scripts/diagnostics/check_route_api.py",
    "scripts/diagnostics/check_subway_data.py",
    "scripts/diagnostics/check_subway_master.py",
    "scripts/diagnostics/check_weather_api.py",
    "scripts/archive/candidate_experiments/check_eunpyeong_daycare_walking_routes.py",
    "scripts/archive/candidate_experiments/validate_yeokchon_mvp_candidate.py",
    "scripts/archive/candidate_experiments/curate_final_mvp_walking_candidate.py",
    "scripts/archive/candidate_experiments/build_walking_mvp_candidates.ps1",
    "scripts/archive/candidate_experiments/build_targeted_osm_flood_avoidance.py",
    "scripts/archive/route_candidate_experiments/build_eunpyeong_od_candidates.ps1",
    "scripts/archive/early_validation/validate_pedestrian_routing_possibility.ps1",
    "scripts/archive/early_validation/analyze_flood_candidates.py",
]
ARCHIVE_MOVED_SCRIPTS = [
    script for script in MOVED_SCRIPTS if "scripts/archive/" in script
]
VALIDATION_MOVED_SCRIPTS = [
    script for script in MOVED_SCRIPTS if "scripts/validation/" in script
]
DIAGNOSTIC_MOVED_SCRIPTS = [
    script for script in MOVED_SCRIPTS if "scripts/diagnostics/" in script
]


def rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("/", "\\")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def classify(path: Path) -> str:
    rel_path = rel(path)
    name = path.name
    if "\\archive\\" in rel_path:
        return "ARCHIVE"
    if "\\diagnostics\\" in rel_path or name.startswith("check_"):
        return "DIAGNOSTIC"
    if "\\validation\\" in rel_path or name.startswith("validate_"):
        return "VALIDATION"
    if name in {"build_osm_walking_routing.py", "project_cleanup.py"}:
        return "PIPELINE_SUPPORT"
    if name.startswith(("prepare_", "process_", "build_route_", "build_osm_")):
        return "PIPELINE_SUPPORT"
    if name == "run_pipeline.ps1":
        return "CORE"
    return "UNKNOWN"


def purpose_for(path: Path, text: str, category: str) -> str:
    name = path.name
    if name == "run_pipeline.ps1":
        return "최종 frozen MVP 검증 wrapper"
    if name == "validate_final_e2e_mvp.py":
        return "최종 end-to-end MVP 시나리오 검증 및 산출물 생성"
    if name == "project_cleanup.py":
        return "최종 시나리오 동결, cleanup inventory/report 생성"
    if name.startswith("check_"):
        return "외부 API 또는 원천 데이터 연결/스키마 진단"
    if name.startswith("process_") or name.startswith("prepare_"):
        return "원천 데이터 전처리"
    if name.startswith("build_route"):
        return "지하철 경로와 침수/접근성 결합 데이터 생성"
    if name == "build_osm_walking_routing.py":
        return "OSM 보행망 구축/검증 및 보행경로 분석 공통 로직"
    if category == "ARCHIVE":
        return "최종 후보 선정 전 실험/검증 과정 보존"
    return "역할 확인 필요"


def parse_python(path: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    imports: list[str] = []
    env_vars: list[str] = []
    data_inputs: list[str] = []
    data_outputs: list[str] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return imports, env_vars, data_inputs, data_outputs
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "getenv" and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    env_vars.append(node.args[0].value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.replace("/", "\\")
            if "data\\raw" in value:
                data_inputs.append(value)
            if "data\\processed" in value:
                data_outputs.append(value)
    return sorted(set(imports)), sorted(set(env_vars)), sorted(set(data_inputs)), sorted(set(data_outputs))


def inspect_script(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    category = classify(path)
    imports, env_vars, data_inputs, data_outputs = parse_python(path) if path.suffix == ".py" else ([], [], [], [])
    if path.suffix == ".ps1":
        env_vars = sorted(set(re.findall(r"\$env:([A-Z0-9_]+)", text)))
        data_inputs = sorted(set(match.replace("/", "\\") for match in re.findall(r"data[/\\]raw[/\\][^\"'\s]+", text)))
        data_outputs = sorted(set(match.replace("/", "\\") for match in re.findall(r"data[/\\]processed[/\\][^\"'\s]+", text)))
    api_usage = sorted(
        {
            token
            for token in ["requests", "apis.data.go.kr", "apihub.kma.go.kr", "ALAN_API_KEY", "ROUTE_API_KEY", "ACCESSIBILITY_API_KEY"]
            if token in text
        }
    )
    calls = sorted(set(re.findall(r"from\s+([a-zA-Z0-9_]+)\s+import|import\s+([a-zA-Z0-9_]+)", text)))
    calls = sorted({item for pair in calls for item in pair if item and item.startswith(("build_", "validate_", "check_"))})
    return {
        "filename": rel(path),
        "language": "PowerShell" if path.suffix == ".ps1" else "Python" if path.suffix == ".py" else path.suffix,
        "purpose": purpose_for(path, text, category),
        "inputs": data_inputs,
        "outputs": data_outputs,
        "api_usage": api_usage,
        "environment_variables": env_vars,
        "imports": imports[:80],
        "called_by": [],
        "calls": calls,
        "final_mvp_dependency": category in {"CORE", "PIPELINE_SUPPORT", "VALIDATION"},
        "reproducibility_value": category in {"CORE", "PIPELINE_SUPPORT", "VALIDATION", "ARCHIVE"},
        "superseded": category in {"ARCHIVE", "DUPLICATE"},
        "duplicate_logic": "후보 탐색/검증 계열은 OSM routing, flood intersection 로직 일부를 중복 보유" if category == "ARCHIVE" else "",
        "recommended_classification": category,
    }


def build_inventory() -> list[dict]:
    scripts = sorted([p for p in SCRIPTS_DIR.rglob("*") if p.is_file() and p.suffix in {".py", ".ps1"}])
    inventory = [inspect_script(path) for path in scripts]
    by_module = {Path(item["filename"]).stem: item["filename"] for item in inventory}
    for item in inventory:
        called_by = []
        for other in inventory:
            if other is item:
                continue
            if Path(item["filename"]).stem in other["calls"]:
                called_by.append(other["filename"])
        item["called_by"] = sorted(called_by)
    return inventory


def freeze_scenario() -> dict:
    report = read_json(E2E_DIR / "e2e_validation_report.json")
    journey = read_json(E2E_DIR / "e2e_full_journey.json")
    scenario = {
        "scenario_id": "move_on_frozen_mvp_eunpyeong_nokbeon_bulgwang_v1",
        "status": "FROZEN",
        "freeze_case": report["case"],
        "origin": {
            "name": "은평구청",
            "address": report["origin"]["address"],
        },
        "origin_coordinate": {
            "lon": report["origin"]["lon"],
            "lat": report["origin"]["lat"],
            "source_record_name": report["origin"]["source_record_name"],
            "source_id": report["origin"]["source_id"],
        },
        "first_mile": {
            "mode": "walk",
            "origin": "은평구청",
            "destination_station": "녹번역",
            "destination_line": "3호선",
            "distance_m": report["first_mile"]["route_length_m"],
            "historical_flood_trace_overlap_m": report["first_mile"]["flood_overlap_length_m_total"],
            "routing_success": report["first_mile"]["routing_success"],
            "data_source": "data\\raw\\osm\\eunpyeong_walk.graphml",
        },
        "departure_station": {
            "station": "녹번",
            "line": "3호선",
            "station_code": journey["scenario"]["first_station"]["station_code"],
            "coordinate": {
                "lon": journey["scenario"]["first_station"]["lon"],
                "lat": journey["scenario"]["first_station"]["lat"],
            },
        },
        "subway_segment": report["subway_segment"],
        "arrival_station": {
            "station": "불광",
            "line": "3호선",
            "station_code": journey["scenario"]["subway_destination_station"]["station_code"],
            "coordinate": {
                "lon": journey["scenario"]["subway_destination_station"]["lon"],
                "lat": journey["scenario"]["subway_destination_station"]["lat"],
            },
        },
        "last_mile_baseline": {
            "mode": "walk",
            "origin_station": "불광역",
            "origin_line": "3호선",
            "destination": "불광제1동주민센터",
            "distance_m": report["last_mile"]["baseline_distance_m"],
            "historical_flood_trace_overlap_m": report["last_mile"]["baseline_historical_flood_overlap_m"],
        },
        "last_mile_alternative": {
            "mode": "walk",
            "description": "다년도 침수흔적 geometry 중첩이 더 적은 실제 OSM 기반 대체 보행경로 후보",
            "distance_m": report["last_mile"]["alternative_distance_m"],
            "extra_distance_m": report["last_mile"]["extra_distance_m"],
            "historical_flood_trace_overlap_m": report["last_mile"]["alternative_historical_flood_overlap_m"],
            "overlap_reduction_m": report["last_mile"]["overlap_reduction_m"],
            "hausdorff_distance_m": report["last_mile"]["hausdorff_distance_m"],
            "method": report["last_mile"]["method"],
        },
        "destination": journey["scenario"]["final_destination"],
        "direct_walk_reference": {
            "distance_m": report["direct_walk_reference"]["route_length_m"],
            "historical_flood_trace_overlap_m": report["direct_walk_reference"]["flood_overlap_length_m_total"],
        },
        "total_walking_distance_with_subway_m": journey["total_walking_distance_with_subway_m"],
        "walking_distance_reduction_reference_m": journey["walking_distance_reduction_reference_m"],
        "accessibility_summary": {
            "녹번_3호선": report["nokbeon_accessibility"],
            "불광_3호선": report["bulgwang_accessibility"],
        },
        "validation_status": {
            "case": report["case"],
            "message": report["final_message"],
            "source_of_truth": [
                "data\\processed\\final_mvp\\end_to_end_validation\\e2e_validation_report.json",
                "data\\processed\\final_mvp\\end_to_end_validation\\e2e_full_journey.json",
                "data\\processed\\final_mvp\\end_to_end_validation\\e2e_full_journey.geojson",
            ],
        },
        "limitations": [
            "flood trace는 침수확률이 아니다.",
            "historical overlap 감소는 안전 보장을 의미하지 않는다.",
            "elevator/lift 정보는 시설 존재 여부이며 실시간 시설 운영 상태가 아니다.",
            "totalReqHr 단위는 아직 검증되지 않았으므로 raw 값으로 유지한다.",
            "slope는 의미/단위 검증 부족으로 안전판정에 사용하지 않는다.",
            "OSM accessibility tag coverage 부족으로 안전판정에 사용하지 않는다.",
            "rainfall scenario는 아직 결합되지 않았다.",
        ],
    }
    write_json(SCENARIO_JSON, scenario)
    return scenario


def build_dependency_graph(inventory: list[dict]) -> dict:
    nodes = [
        {
            "script": item["filename"],
            "classification": item["recommended_classification"],
            "inputs": item["inputs"],
            "outputs": item["outputs"],
            "calls": item["calls"],
        }
        for item in inventory
    ]
    final_chain = [
        "data\\raw\\flood + data\\raw\\subway + data\\raw\\accessibility + data\\raw\\mobility",
        "scripts\\prepare_flood_data.py / scripts\\process_subway_data.py / scripts\\process_mobility_geojson.ps1",
        "data\\raw\\osm\\eunpyeong_walk.graphml",
        "scripts\\build_osm_walking_routing.py",
        "data\\processed\\final_mvp\\end_to_end_validation\\e2e_validation_report.json",
        "scripts\\validation\\validate_final_e2e_mvp.py",
        "data\\processed\\final_mvp\\final_mvp_scenario.json",
    ]
    return {"nodes": nodes, "final_frozen_scenario_dependency_chain": final_chain}


def smoke_regression(scenario: dict) -> dict:
    report = read_json(E2E_DIR / "e2e_validation_report.json")
    def close(actual: float, expected: float, tolerance: float = 0.05) -> bool:
        return abs(float(actual) - expected) <= tolerance
    checks = {
        "osm_graph_exists": (PROJECT_ROOT / "data/raw/osm/eunpyeong_walk.graphml").exists(),
        "final_mvp_scenario_json_load": SCENARIO_JSON.exists(),
        "e2e_validation_report_load": bool(report),
        "journey_segments_connected": [
            seg["validation_status"] for seg in read_json(E2E_DIR / "e2e_full_journey.json")["segments"]
        ] == ["PASS", "PASS", "PASS"],
        "first_mile_distance": close(report["first_mile"]["route_length_m"], 462.059),
        "first_mile_overlap": close(report["first_mile"]["flood_overlap_length_m_total"], 0.0),
        "subway_totalDstc": report["subway_segment"]["totalDstc"] == 1100,
        "subway_totalReqHr_raw": report["subway_segment"]["totalReqHr_raw"] == 90,
        "subway_transfer_count": report["subway_segment"]["transfer_count"] == 0,
        "last_mile_baseline": close(report["last_mile"]["baseline_distance_m"], 331.712),
        "last_mile_alternative": close(report["last_mile"]["alternative_distance_m"], 333.743),
        "last_mile_extra": close(report["last_mile"]["extra_distance_m"], 2.031),
        "last_mile_overlap_reduction": close(report["last_mile"]["overlap_reduction_m"], 9.985),
        "last_mile_hausdorff": close(report["last_mile"]["hausdorff_distance_m"], 29.228),
        "direct_walk": close(report["direct_walk_reference"]["route_length_m"], 1376.254),
        "total_walking": close(scenario["total_walking_distance_with_subway_m"], 795.802),
    }
    return {"checks": checks, "passed": all(checks.values()), "tolerance_m": 0.05}


def write_readme() -> None:
    README.write_text(
        """# MOVE:ON

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
pwsh -ExecutionPolicy Bypass -File scripts\\run_pipeline.ps1
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
""",
        encoding="utf-8",
    )


def write_run_pipeline() -> None:
    RUN_PIPELINE.write_text(
        """$ErrorActionPreference = "Stop"

$BundledPython = Join-Path $env:USERPROFILE ".cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe"
if (Test-Path $BundledPython) {
    $Python = $BundledPython
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "Python interpreter not found."
    }
    $Python = $PythonCommand.Source
}

& $Python scripts\\validation\\validate_final_e2e_mvp.py
& $Python scripts\\project_cleanup.py --report-only
""",
        encoding="utf-8",
    )


def main() -> None:
    CLEANUP_DIR.mkdir(parents=True, exist_ok=True)
    scenario = freeze_scenario()
    write_readme()
    write_run_pipeline()

    inventory = build_inventory()
    write_json(SCRIPT_INVENTORY_JSON, inventory)
    graph = build_dependency_graph(inventory)
    write_json(DEPENDENCY_GRAPH_JSON, graph)

    by_class: dict[str, list[str]] = {}
    for item in inventory:
        by_class.setdefault(item["recommended_classification"], []).append(item["filename"])
    write_json(CLASSIFICATION_JSON, by_class)

    top_level_count = len([p for p in SCRIPTS_DIR.glob("*") if p.is_file() and p.suffix in {".py", ".ps1"}])
    regression = smoke_regression(scenario)
    cleanup_report = {
        "scripts_count_before_cleanup": BEFORE_SCRIPT_COUNT,
        "top_level_scripts_count_after_cleanup": top_level_count,
        "classifications": by_class,
        "archive_moved_scripts": ARCHIVE_MOVED_SCRIPTS,
        "validation_moved_scripts": VALIDATION_MOVED_SCRIPTS,
        "diagnostic_moved_scripts": DIAGNOSTIC_MOVED_SCRIPTS,
        "deleted_items": ["scripts\\__pycache__"],
        "moved_or_integrated_items": MOVED_SCRIPTS
        + ["scripts/validation/validate_final_e2e_mvp.py import dependency made independent"],
        "final_execution_order": [
            "pwsh -ExecutionPolicy Bypass -File scripts\\run_pipeline.ps1",
            "or: python scripts\\validation\\validate_final_e2e_mvp.py",
            "then inspect data\\processed\\final_mvp\\final_mvp_scenario.json",
        ],
        "regression": regression,
        "frozen_scenario_preserved": regression["passed"] and SCENARIO_JSON.exists(),
        "gitignore_env_ignored": ".env" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore").splitlines(),
        "remaining_todo": [
            "과거 실제 폭우 이벤트 선정 및 시간대별 강수 demo dataset 구축",
            "Risk Score 또는 동등한 위험상태 판단",
            "Mobility Failure Point",
            "Last Accessible Departure",
            "Alan explanation",
            "final UI integration",
        ],
    }
    write_json(CLEANUP_REPORT_JSON, cleanup_report)

    if "--report-only" not in __import__("sys").argv:
        print("1. frozen scenario")
        print("- 은평구청 -> 녹번역 3호선 -> 불광역 3호선 -> 불광제1동주민센터")
        print("2. 정리 전/후 scripts 구조")
        print(f"- before: {BEFORE_SCRIPT_COUNT}, after top-level: {top_level_count}")
        print("3. CORE scripts")
        print("- " + ", ".join(by_class.get("CORE", [])))
        print("4. validation scripts")
        print("- " + ", ".join(by_class.get("VALIDATION", [])))
        print("5. archive 이동 scripts")
        print("- " + ", ".join(ARCHIVE_MOVED_SCRIPTS))
        print("6. 실제 삭제 항목")
        print("- scripts\\__pycache__")
        print("7. 최종 실행 순서")
        print("- pwsh -ExecutionPolicy Bypass -File scripts\\run_pipeline.ps1")
        print("8. regression 결과")
        print(f"- passed: {regression['passed']}")
        print("9. final scenario metric 보존 여부")
        print(f"- {cleanup_report['frozen_scenario_preserved']}")
        print("10. 다음 개발 단계")
        print("- 과거 실제 폭우 이벤트 선정 및 시간대별 강수 demo dataset 구축")
        print("다음 개발 단계:")
        print("과거 실제 폭우 이벤트 선정 및 시간대별 강수 demo dataset 구축")


if __name__ == "__main__":
    main()
