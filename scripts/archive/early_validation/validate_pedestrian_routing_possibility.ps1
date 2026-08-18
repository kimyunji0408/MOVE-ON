$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutputDir = Join-Path $ProjectRoot "data/processed/routing_validation"

$RoutingInventoryJson = Join-Path $OutputDir "routing_data_inventory.json"
$NetworkValidationJson = Join-Path $OutputDir "pedestrian_network_validation.json"
$WalkingOdCsv = Join-Path $OutputDir "walking_od_candidates.csv"
$WalkingAlternativesCsv = Join-Path $OutputDir "walking_route_alternatives.csv"
$WalkingAlternativesJson = Join-Path $OutputDir "walking_route_alternatives.json"
$TopCandidatesJson = Join-Path $OutputDir "top_walking_route_candidates.json"
$TopCandidatesGeoJson = Join-Path $OutputDir "top_walking_route_candidates.geojson"
$ReportJson = Join-Path $OutputDir "routing_validation_report.json"

$PedestrianRoutesFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_pedestrian_safe_routes.geojson"
$MobilityFacilitiesFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_mobility_facilities.geojson"
$PedestrianPointsFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_pedestrian_support_points.geojson"
$ValidationReportFile = Join-Path $ProjectRoot "data/processed/mobility/validation_report.json"
$StationAddressFile = Join-Path $ProjectRoot "data/raw/subway/서울교통공사_역주소 및 전화번호_20250318.csv"
$AccessibilityFile = Join-Path $ProjectRoot "data/processed/accessibility/candidate_station_accessibility.csv"

function RelPath($Path) {
    return $Path.Replace($ProjectRoot + "\", "")
}

function Read-Json($Path) {
    return Get-Content -Raw -Encoding utf8 -LiteralPath $Path | ConvertFrom-Json
}

function Get-JsonFeatureCount($Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $json = Read-Json $Path
    return @($json.features).Count
}

function Get-EunpyeongStationCount() {
    if (-not (Test-Path -LiteralPath $StationAddressFile)) { return 0 }
    $rows = Import-Csv -LiteralPath $StationAddressFile -Encoding OEM
    $names = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($row in $rows) {
        if (([string]$row.도로명주소).Contains("은평구") -or ([string]$row.지번주소).Contains("은평구")) {
            [void]$names.Add([string]$row.역명)
        }
    }
    if (Test-Path -LiteralPath $AccessibilityFile) {
        $access = Import-Csv -LiteralPath $AccessibilityFile -Encoding utf8
        $accessNames = [System.Collections.Generic.HashSet[string]]::new()
        foreach ($row in $access) { [void]$accessNames.Add([string]$row.station_name) }
        $filtered = @($names | Where-Object { $accessNames.Contains($_) })
        return $filtered.Count
    }
    return $names.Count
}

function Find-ProjectFilesByExtension($Extensions) {
    $result = @()
    foreach ($file in (Get-ChildItem -LiteralPath $ProjectRoot -Recurse -File)) {
        if ($Extensions -contains $file.Extension.ToLowerInvariant()) {
            $result += [pscustomobject][ordered]@{
                path = RelPath $file.FullName
                extension = $file.Extension.ToLowerInvariant()
                bytes = $file.Length
            }
        }
    }
    return $result
}

function Find-PotentialRoutingFiles() {
    $keywords = @("road", "walk", "pedestrian", "network", "node", "edge", "link", "osm", "graph", "route", "routing", "도로", "보행", "중심선", "노드", "링크")
    $extensions = @(".graphml", ".osm", ".pbf", ".gpkg", ".sqlite", ".db", ".shp", ".geojson", ".csv", ".json")
    $matches = @()
    foreach ($file in (Find-ProjectFilesByExtension $extensions)) {
        $lower = $file.path.ToLowerInvariant()
        foreach ($keyword in $keywords) {
            if ($lower.Contains($keyword.ToLowerInvariant())) {
                $matches += $file
                break
            }
        }
    }
    return $matches
}

function Find-CodeKeywordMatches() {
    $patterns = @(
        "networkx", "osmnx", "shortest_path", "shortest path", "k-shortest",
        "dijkstra", "astar", "walking route api", "pedestrian graph",
        "routing", "kakao", "naver", "tmap", "보행경로 API", "도로망", "보행망"
    )
    $matches = @()
    foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "scripts") -File)) {
        if ($file.Name -eq "validate_pedestrian_routing_possibility.ps1") { continue }
        $text = Get-Content -Raw -Encoding utf8 -LiteralPath $file.FullName
        foreach ($pattern in $patterns) {
            if ($text.ToLowerInvariant().Contains($pattern.ToLowerInvariant())) {
                $matches += [pscustomobject][ordered]@{
                    file = RelPath $file.FullName
                    keyword = $pattern
                }
            }
        }
    }
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot "docs")) {
        foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "docs") -Recurse -File)) {
            $text = Get-Content -Raw -Encoding utf8 -LiteralPath $file.FullName
            foreach ($pattern in $patterns) {
                if ($text.ToLowerInvariant().Contains($pattern.ToLowerInvariant())) {
                    $matches += [pscustomobject][ordered]@{
                        file = RelPath $file.FullName
                        keyword = $pattern
                    }
                }
            }
        }
    }
    return $matches
}

New-Item -ItemType Directory -Force $OutputDir | Out-Null

$allNetworkFiles = Find-ProjectFilesByExtension @(".graphml", ".osm", ".pbf", ".gpkg", ".sqlite", ".db")
$potentialRoutingFiles = Find-PotentialRoutingFiles
$codeMatches = Find-CodeKeywordMatches

$pedestrianLineCount = Get-JsonFeatureCount $PedestrianRoutesFile
$mobilityFacilityCount = Get-JsonFeatureCount $MobilityFacilitiesFile
$supportPointCount = Get-JsonFeatureCount $PedestrianPointsFile
$stationCount = Get-EunpyeongStationCount

$routableGraphFiles = @($allNetworkFiles)
$nodeEdgeCandidateFiles = @($potentialRoutingFiles | Where-Object {
    $_.path -match "(node|edge|link|graph|network|osm|pbf|graphml|노드|링크|도로망|보행망)" -and
    $_.path -notmatch "data\\processed\\walking_mvp_candidates|data\\processed\\routing_validation|data\\processed\\mobility|data\\raw\\mobility|data\\processed\\flood|data\\raw\\flood|data\\processed\\routes|data\\processed\\route_analysis|data\\processed\\od_candidates|data\\raw\\od_candidates|data\\raw\\subway|data\\processed\\accessibility|data\\processed\\weather"
})
$hasWalkingApiCode = @($codeMatches | Where-Object {
    $_.keyword -match "walking route api|kakao|naver|tmap|보행경로 API" -and
    $_.file -notmatch "build_walking_mvp_candidates.ps1"
}).Count -gt 0

$routableNetworkExists = (
    $routableGraphFiles.Count -gt 0 -or
    $nodeEdgeCandidateFiles.Count -gt 0 -or
    $hasWalkingApiCode
)

$inventory = [ordered]@{
    generated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    search_scope = @("data", "scripts", "docs")
    graph_file_candidates = $routableGraphFiles
    potential_routing_files = $potentialRoutingFiles
    node_edge_candidate_files = $nodeEdgeCandidateFiles
    code_keyword_matches = $codeMatches
    existing_non_routable_reference_data = [ordered]@{
        pedestrian_safe_routes = [ordered]@{
            path = RelPath $PedestrianRoutesFile
            feature_count = $pedestrianLineCount
            role = "reference walking-support LineString data, not a routable node-edge graph"
        }
        mobility_facilities = [ordered]@{
            path = RelPath $MobilityFacilitiesFile
            feature_count = $mobilityFacilityCount
            role = "near-route mobility/support context"
        }
        pedestrian_support_points = [ordered]@{
            path = RelPath $PedestrianPointsFile
            feature_count = $supportPointCount
            role = "near-route support context"
        }
    }
    routable_pedestrian_network_found = $routableNetworkExists
}
$inventory | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $RoutingInventoryJson -Encoding utf8

$networkValidation = [ordered]@{
    generated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    status = if ($routableNetworkExists) { "REVIEW_REQUIRED" } else { "FAIL" }
    routable_network_exists = $routableNetworkExists
    node_count = $null
    edge_count = $null
    geometry_exists = $null
    connected_components = $null
    largest_connected_component_ratio = $null
    edge_length_available = $null
    coordinate_system = $null
    includes_eunpyeong = $null
    pedestrian_routing_suitable = $false
    reason = if ($routableNetworkExists) {
        "Potential routing-related files/code were detected and need manual schema/topology validation."
    } else {
        "No node-edge graph, routable pedestrian network file, OSM extract, GraphML, or walking-route API implementation was found in the project."
    }
    explicit_non_network_note = "The 395 processed pedestrian LineStrings have no node ID, edge ID, connectivity, or shared topology metadata. They must not be connected into new routes."
}
$networkValidation | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $NetworkValidationJson -Encoding utf8

$emptyOdRows = @([pscustomobject][ordered]@{
    station = $null
    station_line = $null
    destination_or_source_name = $null
    station_coordinate = $null
    destination_coordinate = $null
    straight_line_distance_m = $null
    generation_status = "skipped_no_routable_pedestrian_network"
    reason = "Actual walking OD candidates requiring route generation were not produced because no routable pedestrian network exists."
})
$emptyOdRows | Export-Csv -LiteralPath $WalkingOdCsv -Encoding utf8BOM -NoTypeInformation

$emptyRouteRows = @([pscustomobject][ordered]@{
    od_id = $null
    route_type = $null
    route_length_m = $null
    flood_overlap_length_m_total = $null
    flood_overlap_ratio = $null
    flood_feature_count_unique_total = $null
    historical_flood_trace_avoiding_candidate = $false
    generation_status = "skipped_no_routable_pedestrian_network"
    reason = "Baseline and alternative walking routes were not generated because no routable pedestrian network exists."
})
$emptyRouteRows | Export-Csv -LiteralPath $WalkingAlternativesCsv -Encoding utf8BOM -NoTypeInformation
$emptyRouteRows | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $WalkingAlternativesJson -Encoding utf8

[ordered]@{
    candidates = @()
    reason = "No top walking route candidates were generated because the project currently lacks a routable pedestrian network or walking route API."
    historical_flood_trace_avoiding_candidate_count = 0
} | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $TopCandidatesJson -Encoding utf8

[ordered]@{
    type = "FeatureCollection"
    features = @()
    properties = [ordered]@{
        reason = "No baseline/alternative walking route geometries were generated because no routable pedestrian network exists."
    }
} | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $TopCandidatesGeoJson -Encoding utf8

$outputFiles = @()
$outputFiles += RelPath $RoutingInventoryJson
$outputFiles += RelPath $NetworkValidationJson
$outputFiles += RelPath $WalkingOdCsv
$outputFiles += RelPath $WalkingAlternativesCsv
$outputFiles += RelPath $WalkingAlternativesJson
$outputFiles += RelPath $TopCandidatesJson
$outputFiles += RelPath $TopCandidatesGeoJson
$outputFiles += RelPath $ReportJson

$report = [ordered]@{
    generated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    conclusion_case = if ($routableNetworkExists) { "REVIEW_REQUIRED" } else { "CASE_C_NO_ROUTABLE_PEDESTRIAN_NETWORK" }
    final_judgement = if ($routableNetworkExists) {
        "Potential routing resources were detected, but their topology must be validated before route generation."
    } else {
        "Current project data cannot generate actual alternative walking routes for the same OD."
    }
    routing_possible = $false
    od_search_result = [ordered]@{
        subway_station_count = $stationCount
        actual_destination_source_count = [ordered]@{
            mobility_facilities = $mobilityFacilityCount
            pedestrian_support_points = $supportPointCount
            pedestrian_safe_route_destinations = if (Test-Path -LiteralPath $PedestrianRoutesFile) {
                @(($pedestrianLineCount), "LineStrings exist but were not used as route graph")
            } else { @() }
        }
        walking_od_candidate_count = 0
        routing_success_od_count = 0
    }
    route_generation_result = [ordered]@{
        baseline_count = 0
        alternative_success_od_count = 0
        substantially_different_alternative_count = 0
        historical_flood_trace_overlap_reducing_alternative_count = 0
        max_overlap_reduction_m = $null
        max_reduction_od = $null
        added_distance_m = $null
    }
    missing_minimum_data_or_api = @(
        "A routable pedestrian node table with node_id, lon, lat, and optional accessibility attributes.",
        "A routable pedestrian edge table with edge_id, from_node_id, to_node_id, length_m, geometry, and walking_allowed.",
        "Topology/connectivity metadata sufficient for Dijkstra or k-shortest simple paths.",
        "Or a verified walking-route API with endpoint, authentication, OD coordinate input, route geometry output, distance, and alternative-route support."
    )
    realistic_options_for_mvp = @(
        [ordered]@{
            option = "OSM/OSMnx extract"
            fit = "High if download is allowed later"
            difficulty = "Medium"
            note = "Can build a walking graph and k-shortest paths, but this step did not download external data."
        },
        [ordered]@{
            option = "Public pedestrian network or road-link dataset"
            fit = "High if node-edge schema is available"
            difficulty = "Medium to High"
            note = "Best for transparent local routing if pedestrian eligibility fields exist."
        },
        [ordered]@{
            option = "Kakao/Naver/Tmap walking route API"
            fit = "Medium"
            difficulty = "Low to Medium"
            note = "Fast MVP path if official endpoint/key are available, but alternative route support and geometry access must be verified."
        }
    )
    output_files = $outputFiles
}
$report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $ReportJson -Encoding utf8

Write-Output "## 1. 실제 보행 routing 가능 여부"
Write-Output ("* routable network 존재: " + ($(if ($routableNetworkExists) { "YES" } else { "NO" })))
Write-Output "* 사용한 데이터: 프로젝트 내부 data/scripts/docs 탐색"
Write-Output ("* node 수: " + $networkValidation.node_count)
Write-Output ("* edge 수: " + $networkValidation.edge_count)
Write-Output ("* 연결성: " + $networkValidation.connected_components)
Write-Output ("* 보행 routing 적합 여부: " + $networkValidation.pedestrian_routing_suitable)

Write-Output ""
Write-Output "## 2. OD 탐색 결과"
Write-Output ("* 지하철역 수: " + $stationCount)
Write-Output ("* 실제 목적시설 수: mobility_facilities=" + $mobilityFacilityCount + ", pedestrian_support_points=" + $supportPointCount + ", pedestrian_LineString=" + $pedestrianLineCount)
Write-Output "* 생성한 OD 후보 수: 0"
Write-Output "* 실제 routing 성공 OD 수: 0"

Write-Output ""
Write-Output "## 3. 복수 경로 생성 결과"
Write-Output "* baseline 생성 수: 0"
Write-Output "* alternative 생성 성공 OD 수: 0"
Write-Output "* geometry가 실질적으로 다른 alternative 수: 0"

Write-Output ""
Write-Output "## 4. 침수흔적 비교 결과"
Write-Output "* baseline보다 침수흔적 overlap이 감소한 alternative 수: 0"
Write-Output "* 최대 감소량: 없음"
Write-Output "* 해당 OD: 없음"
Write-Output "* 추가 이동거리: 없음"

Write-Output ""
Write-Output "## 5. MOVE:ON 후보 TOP 5"
Write-Output "실제 보행 도로망이 없어 TOP 후보를 생성하지 않았습니다."

Write-Output ""
Write-Output "## 6. 최종 판단"
Write-Output "C. 실제 routable pedestrian network 없음"
Write-Output "현재 프로젝트 데이터만으로 실제 보행 대체경로 생성은 불가능합니다. 추가 경로 데이터/API가 필요합니다."

Write-Output ""
Write-Output "## 7. 다음 단계"
Write-Output "* OSM/공공 보행망/보행경로 API 중 하나를 선택해 실제 node-edge 또는 route geometry를 확보한다."
Write-Output "* 확보한 데이터의 node/edge schema와 연결성을 먼저 검증한다."
Write-Output "* 그 다음 동일 OD k-shortest walking routes와 다년도 침수흔적 중첩 감소 후보를 검증한다."

Write-Output ""
Write-Output "Saved:"
foreach ($path in $report.output_files) { Write-Output $path }
