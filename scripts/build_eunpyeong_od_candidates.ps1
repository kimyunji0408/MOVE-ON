$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutputDir = Join-Path $ProjectRoot "data/processed/od_candidates"
$RawCacheDir = Join-Path $ProjectRoot "data/raw/od_candidates"
$RouteCacheFile = Join-Path $RawCacheDir "route_api_cache.json"

$ValidationReportFile = Join-Path $ProjectRoot "data/processed/mobility/validation_report.json"
$StationMasterFile = Join-Path $ProjectRoot "data/raw/subway/서울시 역사마스터 정보.csv"
$StationAddressFile = Join-Path $ProjectRoot "data/raw/subway/서울교통공사_역주소 및 전화번호_20250318.csv"
$AccessibilityFile = Join-Path $ProjectRoot "data/processed/accessibility/candidate_station_accessibility.csv"
$FloodFile = Join-Path $ProjectRoot "data/processed/flood/seoul_flood_trace_2022_2025.geojson"
$MobilityFacilitiesFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_mobility_facilities.geojson"
$PedestrianRoutesFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_pedestrian_safe_routes.geojson"
$PedestrianPointsFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_pedestrian_support_points.geojson"

$CandidatesCsv = Join-Path $OutputDir "eunpyeong_od_route_candidates.csv"
$CandidatesJson = Join-Path $OutputDir "eunpyeong_od_route_candidates.json"
$SummaryJson = Join-Path $OutputDir "eunpyeong_od_comparison_summary.json"
$TopJson = Join-Path $OutputDir "top_mvp_candidates.json"

$RouteEndpoint = "https://apis.data.go.kr/B553766/path2/getShtrmPath2"
$SearchDt = "2026-08-14 18:00:00"
$Years = @(2022, 2023, 2024, 2025)
$RadiusMeters = 300

function Read-Json($Path) {
    return Get-Content -Raw -Encoding utf8 -LiteralPath $Path | ConvertFrom-Json
}

function Read-EnvValue($Name) {
    $line = Get-Content -LiteralPath (Join-Path $ProjectRoot ".env") | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
}

function Get-RouteApiKey() {
    $key = Read-EnvValue "ROUTE_API_KEY"
    if ($key -match "%[0-9A-Fa-f]{2}") {
        $key = [System.Uri]::UnescapeDataString($key)
    }
    Write-Host ("[route:key] selected_env_var: ROUTE_API_KEY")
    Write-Host ("[route:key] value_exists: " + [bool]$key)
    Write-Host ("[route:key] string_length: " + $key.Length)
    if (-not $key) { throw "ROUTE_API_KEY was not found." }
    return $key
}

function Read-CsvWithKoreanHeaders($Path) {
    foreach ($encoding in @("utf8", "utf8BOM", "Default", "OEM")) {
        try {
            $rows = Import-Csv -LiteralPath $Path -Encoding $encoding
            if ($rows.Count -gt 0 -and (
                $rows[0].PSObject.Properties.Name -contains "역사명" -or
                $rows[0].PSObject.Properties.Name -contains "역명" -or
                $rows[0].PSObject.Properties.Name -contains "station_name"
            )) {
                return $rows
            }
        } catch {
            continue
        }
    }
    throw "Could not read CSV with expected headers: $Path"
}

function Canonical-Line($LineName) {
    $text = [string]$LineName
    if ($text -match "(\d+)") { return "$([int]$Matches[1])호선" }
    return $text
}

function To-DoubleOrNull($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    $result = 0.0
    if ([double]::TryParse(([string]$Value -replace ",", ""), [ref]$result)) { return $result }
    return $null
}

function Project-ToLocalMeters([double]$Lon, [double]$Lat, [double]$CenterLon, [double]$CenterLat) {
    $earthRadius = 6371008.8
    $degToRad = [Math]::PI / 180.0
    $x = ($Lon - $CenterLon) * $degToRad * $earthRadius * [Math]::Cos($CenterLat * $degToRad)
    $y = ($Lat - $CenterLat) * $degToRad * $earthRadius
    return @($x, $y)
}

function Get-PointDistanceMeters($PointLon, $PointLat, $StationLon, $StationLat) {
    $p = Project-ToLocalMeters $PointLon $PointLat $StationLon $StationLat
    return [Math]::Sqrt($p[0] * $p[0] + $p[1] * $p[1])
}

function Get-DistancePointToSegmentMeters($StationLon, $StationLat, $Lon1, $Lat1, $Lon2, $Lat2) {
    $a = Project-ToLocalMeters $Lon1 $Lat1 $StationLon $StationLat
    $b = Project-ToLocalMeters $Lon2 $Lat2 $StationLon $StationLat
    $abx = $b[0] - $a[0]
    $aby = $b[1] - $a[1]
    $apx = -$a[0]
    $apy = -$a[1]
    $denom = $abx * $abx + $aby * $aby
    if ($denom -eq 0) { return [Math]::Sqrt($apx * $apx + $apy * $apy) }
    $t = ($apx * $abx + $apy * $aby) / $denom
    if ($t -lt 0) { $t = 0 }
    if ($t -gt 1) { $t = 1 }
    $cx = $a[0] + $t * $abx
    $cy = $a[1] + $t * $aby
    return [Math]::Sqrt($cx * $cx + $cy * $cy)
}

function Get-MinDistanceToLineStringMeters($Coordinates, $StationLon, $StationLat) {
    $minDistance = [double]::PositiveInfinity
    for ($i = 0; $i -lt $Coordinates.Count - 1; $i++) {
        $a = $Coordinates[$i]
        $b = $Coordinates[$i + 1]
        $distance = Get-DistancePointToSegmentMeters $StationLon $StationLat ([double]$a[0]) ([double]$a[1]) ([double]$b[0]) ([double]$b[1])
        if ($distance -lt $minDistance) { $minDistance = $distance }
    }
    return $minDistance
}

function Get-CoordinatesFlat($Coordinates, $Output) {
    if ($Coordinates -is [System.Array] -and $Coordinates.Count -ge 2 -and $Coordinates[0] -is [ValueType]) {
        $Output.Add(@([double]$Coordinates[0], [double]$Coordinates[1])) | Out-Null
        return
    }
    foreach ($item in $Coordinates) { Get-CoordinatesFlat $item $Output }
}

function Get-MinDistanceToGeometryMeters($Geometry, $StationLon, $StationLat) {
    if ($Geometry.type -eq "Point") {
        return Get-PointDistanceMeters ([double]$Geometry.coordinates[0]) ([double]$Geometry.coordinates[1]) $StationLon $StationLat
    }
    if ($Geometry.type -eq "LineString") {
        return Get-MinDistanceToLineStringMeters $Geometry.coordinates $StationLon $StationLat
    }
    $coords = New-Object System.Collections.Generic.List[object]
    Get-CoordinatesFlat $Geometry.coordinates $coords
    $minDistance = [double]::PositiveInfinity
    foreach ($coord in $coords) {
        $distance = Get-PointDistanceMeters $coord[0] $coord[1] $StationLon $StationLat
        if ($distance -lt $minDistance) { $minDistance = $distance }
    }
    return $minDistance
}

function Extract-Route($Payload, $RouteId, $Origin, $Destination, $ThroughStation) {
    $body = $Payload.body
    $paths = @($body.paths)
    $segments = @()
    $stations = New-Object System.Collections.Generic.List[string]
    $lines = New-Object System.Collections.Generic.HashSet[string]
    for ($i = 0; $i -lt $paths.Count; $i++) {
        $path = $paths[$i]
        if ($i -eq 0) { $stations.Add([string]$path.dptreStn.stnNm) | Out-Null }
        $stations.Add([string]$path.arvlStn.stnNm) | Out-Null
        [void]$lines.Add([string]$path.dptreStn.lineNm)
        $segments += [pscustomobject][ordered]@{
            departure_station = $path.dptreStn.stnNm
            arrival_station = $path.arvlStn.stnNm
            line = $path.dptreStn.lineNm
            distance_raw = $path.stnSctnDstc
            transfer_yn = $path.trsitYn
        }
    }
    return [pscustomobject][ordered]@{
        route_id = $RouteId
        origin = $Origin
        destination = $Destination
        through_station = $ThroughStation
        station_sequence = @($stations)
        line_sequence = @($lines)
        total_distance = $body.totalDstc
        total_required_time_raw = $body.totalReqHr
        transfer_count = $body.trsitNmtm
        transfer_stations = @($body.trfstnNms)
        segment_count = $segments.Count
        segments = $segments
    }
}

function Route-Signature($Route) {
    return (($Route.station_sequence -join ">") + "|" + ($Route.line_sequence -join ">"))
}

function Call-RouteApi($ApiKey, $Origin, $Destination, $ThroughStation, $Cache) {
    $cacheKey = "$Origin|$Destination|$ThroughStation"
    if ($Cache.ContainsKey($cacheKey) -and $Cache[$cacheKey].ok -eq $true) { return $Cache[$cacheKey] }
    $params = @{
        serviceKey = $ApiKey
        dataType = "JSON"
        dptreStn = $Origin
        arvlStn = $Destination
        searchDt = $SearchDt
        searchType = "duration"
        stationValueType = "name"
        schInclYn = "N"
    }
    if (-not [string]::IsNullOrWhiteSpace($ThroughStation)) { $params.thrghStns = $ThroughStation }
    try {
        $queryParts = @()
        foreach ($paramName in @("serviceKey", "dataType", "dptreStn", "arvlStn", "searchDt", "searchType", "stationValueType", "schInclYn", "thrghStns")) {
            if (-not $params.ContainsKey($paramName)) { continue }
            $queryParts += (
                [System.Net.WebUtility]::UrlEncode($paramName) +
                "=" +
                [System.Net.WebUtility]::UrlEncode([string]$params[$paramName])
            )
        }
        $requestUri = $RouteEndpoint + "?" + ($queryParts -join "&")
        $response = Invoke-WebRequest -Uri $requestUri -Method Get -TimeoutSec 30 -SkipHttpErrorCheck
        $text = [string]$response.Content
        $payload = $null
        try { $payload = $text | ConvertFrom-Json } catch {}
        $result = [pscustomobject][ordered]@{
            ok = ([int]$response.StatusCode -eq 200 -and $null -ne $payload -and $payload.header.resultCode -eq "00" -and @($payload.body.paths).Count -gt 0)
            http_status = [int]$response.StatusCode
            result_code = if ($payload) { $payload.header.resultCode } else { $null }
            result_msg = if ($payload) { $payload.header.resultMsg } else { $null }
            payload = $payload
        }
    } catch {
        $result = [pscustomobject][ordered]@{
            ok = $false
            http_status = $null
            result_code = $null
            result_msg = $_.Exception.GetType().Name
            payload = $null
        }
    }
    $Cache[$cacheKey] = $result
    Start-Sleep -Milliseconds 150
    return $result
}

function Build-Stations() {
    $stationMaster = Read-CsvWithKoreanHeaders $StationMasterFile
    $stationAddress = Import-Csv -LiteralPath $StationAddressFile -Encoding OEM
    $accessRows = Import-Csv -LiteralPath $AccessibilityFile -Encoding utf8
    $accessStationSet = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($row in $accessRows) { [void]$accessStationSet.Add($row.station_name) }
    $eunpyeongNames = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($row in $stationAddress) {
        if (([string]$row.도로명주소).Contains("은평구") -or ([string]$row.지번주소).Contains("은평구")) {
            [void]$eunpyeongNames.Add($row.역명)
        }
    }
    $stations = @()
    foreach ($row in $stationMaster) {
        $name = $row.역사명
        if (-not $eunpyeongNames.Contains($name)) { continue }
        if (-not $accessStationSet.Contains($name)) { continue }
        $lon = To-DoubleOrNull $row.경도
        $lat = To-DoubleOrNull $row.위도
        if ($null -eq $lon -or $null -eq $lat) { continue }
        $line = Canonical-Line $row.호선
        $stations += [pscustomobject][ordered]@{ station = $name; line = $line; lon = $lon; lat = $lat }
    }
    return @($stations | Sort-Object station, line -Unique)
}

function Get-StationCoordinate($StationRows, $Station, $Line) {
    $match = @($StationRows | Where-Object { $_.station -eq $Station -and $_.line -eq $Line } | Select-Object -First 1)
    if ($match.Count -eq 0) {
        $match = @($StationRows | Where-Object { $_.station -eq $Station } | Select-Object -First 1)
    }
    if ($match.Count -eq 0) { return $null }
    return $match[0]
}

function Build-AccessibilityIndex() {
    $rows = Import-Csv -LiteralPath $AccessibilityFile -Encoding utf8
    $index = @{}
    foreach ($group in ($rows | Group-Object station_name, line_name)) {
        $first = $group.Group[0]
        $elev = @($group.Group | Where-Object { $_.facility_type -eq "elevator" }).Count
        $lift = @($group.Group | Where-Object { $_.facility_type -eq "wheelchair_lift" }).Count
        $key = "$($first.station_name)|$($first.line_name)"
        $index[$key] = [pscustomobject][ordered]@{
            elevator_available = $elev -gt 0
            wheelchair_lift_available = $lift -gt 0
            elevator_count = $elev
            wheelchair_lift_count = $lift
        }
    }
    return $index
}

function Get-RouteAccessibility($Route, $AccessIndex) {
    $byStation = @()
    $missingElevator = 0
    for ($i = 0; $i -lt $Route.station_sequence.Count; $i++) {
        $station = $Route.station_sequence[$i]
        $line = if ($Route.line_sequence.Count -eq 1) { $Route.line_sequence[0] } else { $Route.line_sequence[[Math]::Min($i, $Route.line_sequence.Count - 1)] }
        $key = "$station|$line"
        $record = if ($AccessIndex.ContainsKey($key)) { $AccessIndex[$key] } else { $null }
        $elevator = if ($record) { $record.elevator_available } else { $null }
        if ($elevator -ne $true) { $missingElevator += 1 }
        $byStation += [pscustomobject][ordered]@{ station = $station; line = $line; elevator_available = $elevator; wheelchair_lift_available = if ($record) { $record.wheelchair_lift_available } else { $null } }
    }
    return [pscustomobject][ordered]@{
        by_station = $byStation
        missing_elevator_station_count = $missingElevator
        all_stations_have_elevator = $missingElevator -eq 0
    }
}

function Get-FloodExposure($Route, $StationRows, $FloodFeatures) {
    $stationSumIds = New-Object System.Collections.Generic.List[string]
    $uniqueIds = [System.Collections.Generic.HashSet[string]]::new()
    $yearSets = @{}
    foreach ($year in $Years) { $yearSets[[string]$year] = [System.Collections.Generic.HashSet[string]]::new() }
    for ($i = 0; $i -lt $Route.station_sequence.Count; $i++) {
        $station = $Route.station_sequence[$i]
        $line = if ($Route.line_sequence.Count -eq 1) { $Route.line_sequence[0] } else { $Route.line_sequence[[Math]::Min($i, $Route.line_sequence.Count - 1)] }
        $metric = Get-StationFloodMetric $StationRows $FloodFeatures $station $line
        foreach ($match in $metric.matches) {
            $id = [string]$match.id
            $stationSumIds.Add($id) | Out-Null
            [void]$uniqueIds.Add($id)
            $year = [string]$match.year
            if ($yearSets.ContainsKey($year)) {
                [void]$yearSets[$year].Add($id)
            }
        }
    }
    return [pscustomobject][ordered]@{
        flood_unique_2022 = $yearSets["2022"].Count
        flood_unique_2023 = $yearSets["2023"].Count
        flood_unique_2024 = $yearSets["2024"].Count
        flood_unique_2025 = $yearSets["2025"].Count
        flood_unique_total = $uniqueIds.Count
        flood_station_sum = $stationSumIds.Count
        flood_duplicates_removed = $stationSumIds.Count - $uniqueIds.Count
    }
}

function Get-StationFloodMetric($StationRows, $FloodFeatures, $Station, $Line) {
    $cacheKey = "$Station|$Line"
    if ($script:StationFloodCache.ContainsKey($cacheKey)) {
        return $script:StationFloodCache[$cacheKey]
    }
    $coord = Get-StationCoordinate $StationRows $Station $Line
    $matches = @()
    if ($null -ne $coord) {
        foreach ($feature in $FloodFeatures) {
            $distance = Get-MinDistanceToGeometryMeters $feature.geometry $coord.lon $coord.lat
            if ($distance -le $RadiusMeters) {
                $matches += [pscustomobject][ordered]@{
                    id = [string]$feature.properties.__feature_id
                    year = [int]$feature.properties.F_YR
                }
            }
        }
    }
    $metric = [pscustomobject][ordered]@{ matches = $matches }
    $script:StationFloodCache[$cacheKey] = $metric
    return $metric
}

function Get-Coverage($Route, $StationRows, $MobilityFeatures, $PedestrianLines, $PedestrianPoints) {
    $byStation = @()
    $totMob = 0; $totLine = 0; $totPoint = 0
    for ($i = 0; $i -lt $Route.station_sequence.Count; $i++) {
        $station = $Route.station_sequence[$i]
        $line = if ($Route.line_sequence.Count -eq 1) { $Route.line_sequence[0] } else { $Route.line_sequence[[Math]::Min($i, $Route.line_sequence.Count - 1)] }
        $coverage = Get-StationCoverageMetric $StationRows $MobilityFeatures $PedestrianLines $PedestrianPoints $station $line
        $mob = $coverage.mobility_facility_count
        $lineCount = $coverage.pedestrian_safe_route_linestring_count
        $pointCount = $coverage.pedestrian_support_point_count
        $totMob += $mob; $totLine += $lineCount; $totPoint += $pointCount
        $byStation += [pscustomobject][ordered]@{ station = $station; line = $line; mobility_facility_count = $mob; pedestrian_safe_route_linestring_count = $lineCount; pedestrian_support_point_count = $pointCount }
    }
    return [pscustomobject][ordered]@{ by_station = $byStation; mobility_facility_count = $totMob; pedestrian_safe_route_linestring_count = $totLine; pedestrian_support_point_count = $totPoint }
}

function Get-StationCoverageMetric($StationRows, $MobilityFeatures, $PedestrianLines, $PedestrianPoints, $Station, $Line) {
    $cacheKey = "$Station|$Line"
    if ($script:StationCoverageCache.ContainsKey($cacheKey)) {
        return $script:StationCoverageCache[$cacheKey]
    }
    $coord = Get-StationCoordinate $StationRows $Station $Line
    $mob = 0
    $lineCount = 0
    $pointCount = 0
    if ($null -ne $coord) {
        foreach ($feature in $MobilityFeatures) {
            if ($feature.geometry.type -eq "Point" -and (Get-MinDistanceToGeometryMeters $feature.geometry $coord.lon $coord.lat) -le $RadiusMeters) {
                $mob++
            }
        }
        foreach ($feature in $PedestrianLines) {
            if ((Get-MinDistanceToGeometryMeters $feature.geometry $coord.lon $coord.lat) -le $RadiusMeters) {
                $lineCount++
            }
        }
        foreach ($feature in $PedestrianPoints) {
            if ($feature.geometry.type -eq "Point" -and (Get-MinDistanceToGeometryMeters $feature.geometry $coord.lon $coord.lat) -le $RadiusMeters) {
                $pointCount++
            }
        }
    }
    $metric = [pscustomobject][ordered]@{
        mobility_facility_count = $mob
        pedestrian_safe_route_linestring_count = $lineCount
        pedestrian_support_point_count = $pointCount
    }
    $script:StationCoverageCache[$cacheKey] = $metric
    return $metric
}

function Safe-Ratio($Numerator, $Denominator) {
    if ($null -eq $Denominator -or [double]$Denominator -eq 0) { return $null }
    return [Math]::Round(([double]$Numerator / [double]$Denominator), 6)
}

New-Item -ItemType Directory -Force $OutputDir | Out-Null
New-Item -ItemType Directory -Force $RawCacheDir | Out-Null

$validation = Read-Json $ValidationReportFile
$gatePass = (
    $validation.eunpyeong_filtering.status -ne "FAIL" -and
    $validation.geometry_validity.status -ne "FAIL" -and
    $validation.coordinate_system.status -ne "FAIL"
)
if (-not $gatePass) { throw "Phase 1 gate failed. OD search was not run." }

$apiKey = Get-RouteApiKey
$stations = Build-Stations
$stationNames = @($stations | Select-Object -ExpandProperty station -Unique)
$accessIndex = Build-AccessibilityIndex
$flood = Read-Json $FloodFile
$idx = 0
foreach ($feature in $flood.features) { $feature.properties | Add-Member -NotePropertyName "__feature_id" -NotePropertyValue ("flood_" + $idx) -Force; $idx++ }
$mobility = Read-Json $MobilityFacilitiesFile
$pedestrianRoutes = Read-Json $PedestrianRoutesFile
$pedestrianPoints = Read-Json $PedestrianPointsFile

$cache = @{}
if (Test-Path -LiteralPath $RouteCacheFile) {
    $cacheJson = Read-Json $RouteCacheFile
    foreach ($prop in $cacheJson.PSObject.Properties) { $cache[$prop.Name] = $prop.Value }
}

$candidateRows = @()
$comparisonSummaries = @()
$routeMetricCache = @{}
$script:StationFloodCache = @{}
$script:StationCoverageCache = @{}
$baselineSuccess = 0; $alternativeTried = 0; $validAlternativeCount = 0; $apiFailures = 0

for ($oi = 0; $oi -lt $stationNames.Count; $oi++) {
    for ($di = 0; $di -lt $stationNames.Count; $di++) {
        if ($oi -eq $di) { continue }
        $origin = $stationNames[$oi]
        $destination = $stationNames[$di]
        $baseResult = Call-RouteApi $apiKey $origin $destination "" $cache
        if (-not $baseResult.ok) { $apiFailures++; continue }
        $baselineSuccess++
        $baselineRoute = Extract-Route $baseResult.payload "baseline" $origin $destination $null
        $baseSig = Route-Signature $baselineRoute
        if (-not $routeMetricCache.ContainsKey($baseSig)) {
            $routeMetricCache[$baseSig] = [pscustomobject][ordered]@{
                accessibility = Get-RouteAccessibility $baselineRoute $accessIndex
                flood = Get-FloodExposure $baselineRoute $stations $flood.features
                coverage = Get-Coverage $baselineRoute $stations $mobility.features $pedestrianRoutes.features $pedestrianPoints.features
            }
        }
        $baseAccess = $routeMetricCache[$baseSig].accessibility
        $baseFlood = $routeMetricCache[$baseSig].flood
        $baseCoverage = $routeMetricCache[$baseSig].coverage
        $baselineRecord = [pscustomobject][ordered]@{
            od = "$origin->$destination"; route_role = "baseline"; origin = $origin; destination = $destination; through_station = $null
            station_sequence = $baselineRoute.station_sequence; line_sequence = $baselineRoute.line_sequence
            total_distance = $baselineRoute.total_distance; totalReqHr_raw = $baselineRoute.total_required_time_raw; transfer_count = $baselineRoute.transfer_count; segment_count = $baselineRoute.segment_count
            flood = $baseFlood; accessibility = $baseAccess; coverage = $baseCoverage
        }
        $candidateRows += $baselineRecord
        $alternatives = @()
        foreach ($through in $stationNames) {
            if ($through -eq $origin -or $through -eq $destination) { continue }
            $alternativeTried++
            $altResult = Call-RouteApi $apiKey $origin $destination $through $cache
            if (-not $altResult.ok) { $apiFailures++; continue }
            $altRoute = Extract-Route $altResult.payload "alternative" $origin $destination $through
            $isDifferent = (Route-Signature $baselineRoute) -ne (Route-Signature $altRoute)
            $isLoop = (@($altRoute.station_sequence | Sort-Object -Unique).Count -ne $altRoute.station_sequence.Count)
            if (-not $isDifferent) { continue }
            $altSig = Route-Signature $altRoute
            if (-not $routeMetricCache.ContainsKey($altSig)) {
                $routeMetricCache[$altSig] = [pscustomobject][ordered]@{
                    accessibility = Get-RouteAccessibility $altRoute $accessIndex
                    flood = Get-FloodExposure $altRoute $stations $flood.features
                    coverage = Get-Coverage $altRoute $stations $mobility.features $pedestrianRoutes.features $pedestrianPoints.features
                }
            }
            $altAccess = $routeMetricCache[$altSig].accessibility
            $altFlood = $routeMetricCache[$altSig].flood
            $altCoverage = $routeMetricCache[$altSig].coverage
            $distanceDelta = [int]$altRoute.total_distance - [int]$baselineRoute.total_distance
            $timeDelta = [int]$altRoute.total_required_time_raw - [int]$baselineRoute.total_required_time_raw
            $floodDelta = [int]$altFlood.flood_unique_total - [int]$baseFlood.flood_unique_total
            $floodReduction = [int]$baseFlood.flood_unique_total - [int]$altFlood.flood_unique_total
            $missingElevDelta = [int]$altAccess.missing_elevator_station_count - [int]$baseAccess.missing_elevator_station_count
            $isValidMoveOnAlternative = ($floodReduction -gt 0 -and $missingElevDelta -le 0 -and -not $isLoop)
            if ($isValidMoveOnAlternative) { $validAlternativeCount++ }
            $altRecord = [pscustomobject][ordered]@{
                od = "$origin->$destination"; route_role = "alternative"; origin = $origin; destination = $destination; through_station = $through
                station_sequence = $altRoute.station_sequence; line_sequence = $altRoute.line_sequence
                total_distance = $altRoute.total_distance; totalReqHr_raw = $altRoute.total_required_time_raw; transfer_count = $altRoute.transfer_count; segment_count = $altRoute.segment_count
                flood = $altFlood; accessibility = $altAccess; coverage = $altCoverage
                comparison = [pscustomobject][ordered]@{
                    distance_delta = $distanceDelta
                    distance_delta_ratio = Safe-Ratio $distanceDelta $baselineRoute.total_distance
                    req_hr_raw_delta = $timeDelta
                    req_hr_raw_delta_ratio = Safe-Ratio $timeDelta $baselineRoute.total_required_time_raw
                    flood_unique_delta = $floodDelta
                    flood_unique_reduction = $floodReduction
                    flood_unique_reduction_ratio = Safe-Ratio $floodReduction $baseFlood.flood_unique_total
                    missing_elevator_delta = $missingElevDelta
                    baseline_route_different = $isDifferent
                    possible_loop_or_duplicate_station = $isLoop
                    valid_moveon_alternative = $isValidMoveOnAlternative
                }
            }
            $candidateRows += $altRecord
            $alternatives += $altRecord
        }
        $bestAlternative = @($alternatives | Where-Object { $_.comparison.valid_moveon_alternative } | Sort-Object @{Expression={$_.comparison.flood_unique_reduction};Descending=$true}, @{Expression={$_.comparison.distance_delta};Descending=$false}, @{Expression={$_.comparison.req_hr_raw_delta};Descending=$false} | Select-Object -First 1)
        $status = if ($bestAlternative.Count -gt 0) { "safer_alternative_found" } elseif ($alternatives.Count -gt 0) { "alternative_exists_but_not_safer_or_accessibility_worse" } else { "no_distinct_alternative_found" }
        $comparisonSummaries += [pscustomobject][ordered]@{ origin = $origin; destination = $destination; baseline = $baselineRecord; alternatives = $alternatives; best_alternative = if ($bestAlternative.Count -gt 0) { $bestAlternative[0] } else { $null }; candidate_status = $status }
    }
}

$cacheForSave = [ordered]@{}
foreach ($key in $cache.Keys) { $cacheForSave[$key] = $cache[$key] }
$cacheForSave | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $RouteCacheFile -Encoding utf8

$csvRows = @($candidateRows | ForEach-Object {
    [pscustomobject][ordered]@{
        od = $_.od; route_role = $_.route_role; origin = $_.origin; destination = $_.destination; through_station = $_.through_station
        station_sequence = ($_.station_sequence -join ">"); line_sequence = ($_.line_sequence -join ">")
        total_distance = $_.total_distance; totalReqHr_raw = $_.totalReqHr_raw; transfer_count = $_.transfer_count; segment_count = $_.segment_count
        flood_unique_total = $_.flood.flood_unique_total; flood_station_sum = $_.flood.flood_station_sum; flood_duplicates_removed = $_.flood.flood_duplicates_removed
        missing_elevator_station_count = $_.accessibility.missing_elevator_station_count; all_stations_have_elevator = $_.accessibility.all_stations_have_elevator
        mobility_facility_count = $_.coverage.mobility_facility_count; pedestrian_safe_route_linestring_count = $_.coverage.pedestrian_safe_route_linestring_count; pedestrian_support_point_count = $_.coverage.pedestrian_support_point_count
        distance_delta = if ($_.comparison) { $_.comparison.distance_delta } else { $null }
        req_hr_raw_delta = if ($_.comparison) { $_.comparison.req_hr_raw_delta } else { $null }
        flood_unique_reduction = if ($_.comparison) { $_.comparison.flood_unique_reduction } else { $null }
        valid_moveon_alternative = if ($_.comparison) { $_.comparison.valid_moveon_alternative } else { $null }
    }
})

$candidateRows | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $CandidatesJson -Encoding utf8
$csvRows | Export-Csv -LiteralPath $CandidatesCsv -Encoding utf8BOM -NoTypeInformation
$comparisonSummaries | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $SummaryJson -Encoding utf8

$safer = @($candidateRows | Where-Object { $_.route_role -eq "alternative" -and $_.comparison.valid_moveon_alternative } | Sort-Object @{Expression={$_.comparison.flood_unique_reduction};Descending=$true}, @{Expression={$_.comparison.distance_delta};Descending=$false}, @{Expression={$_.comparison.req_hr_raw_delta};Descending=$false} | Select-Object -First 5)
$noSafer = @($comparisonSummaries | Where-Object { $_.candidate_status -ne "safer_alternative_found" } | Select-Object -First 3)
$top = [ordered]@{
    safer_alternative_candidates = @($safer | ForEach-Object {
        $candidate = $_
        $candidateSummary = @($comparisonSummaries | Where-Object { $_.origin -eq $candidate.origin -and $_.destination -eq $candidate.destination } | Select-Object -First 1)
        [pscustomobject][ordered]@{
            origin = $candidate.origin; destination = $candidate.destination; through_station = $candidate.through_station
            baseline_route = ($candidateSummary[0].baseline.station_sequence -join ">")
            alternative_route = ($candidate.station_sequence -join ">")
            baseline_distance = $candidateSummary[0].baseline.total_distance
            alternative_distance = $candidate.total_distance
            distance_delta = $candidate.comparison.distance_delta
            baseline_totalReqHr_raw = $candidateSummary[0].baseline.totalReqHr_raw
            alternative_totalReqHr_raw = $candidate.totalReqHr_raw
            req_hr_raw_delta = $candidate.comparison.req_hr_raw_delta
            baseline_flood_exposure = $candidateSummary[0].baseline.flood.flood_unique_total
            alternative_flood_exposure = $candidate.flood.flood_unique_total
            flood_reduction = $candidate.comparison.flood_unique_reduction
            elevator_accessibility_comparison = [pscustomobject][ordered]@{
                alternative_missing_elevator_station_count = $candidate.accessibility.missing_elevator_station_count
                missing_elevator_delta = $candidate.comparison.missing_elevator_delta
            }
            pedestrian_mobility_data_coverage = $candidate.coverage
            mvp_reason = "기본 경로와 실제 station/line sequence가 다르고, 다년도 침수흔적 노출 수가 감소하며, E/V 미확보역 수가 증가하지 않는다."
        }
    })
    no_safer_alternative_candidates = $noSafer
}
$top | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $TopJson -Encoding utf8

Write-Output "## 1. GeoJSON 검증 결과"
Write-Output ("* Encoding: " + $validation.encoding.status)
Write-Output ("* Eunpyeong filtering: " + $validation.eunpyeong_filtering.status)
Write-Output ("* Geometry: " + $validation.geometry_validity.status)
Write-Output ("* Coordinate system: " + $validation.coordinate_system.status)
Write-Output ("* Slope usability: " + $validation.slope_usability.status)
Write-Output "* 수정: 기존 mobility summary의 은평구 bbox/source bbox 구분을 반영했고, validation_report.json을 사용했다."

Write-Output ""
Write-Output "## 2. OD 탐색 범위"
Write-Output ("* 은평구 후보역 수: " + $stationNames.Count)
Write-Output ("* 생성한 OD 수: " + ($stationNames.Count * ($stationNames.Count - 1)))
Write-Output ("* 성공한 baseline 수: " + $baselineSuccess)
Write-Output ("* 시도한 alternative 수: " + $alternativeTried)
Write-Output ("* 유효 alternative 수: " + $validAlternativeCount)
Write-Output ("* API 실패 수: " + $apiFailures)

Write-Output ""
Write-Output "## 3. MOVE:ON MVP 상위 후보 TOP 5"
$rank = 1
foreach ($item in $safer) {
    $summary = @($comparisonSummaries | Where-Object { $_.origin -eq $item.origin -and $_.destination -eq $item.destination } | Select-Object -First 1)
    Write-Output ("{0}. {1} → {2}" -f $rank, $item.origin, $item.destination)
    Write-Output ("* 기본: 거리 {0} / 시간 raw {1} / 침수흔적 노출 {2} / E/V 미확보역 {3}" -f $summary.baseline.total_distance, $summary.baseline.totalReqHr_raw, $summary.baseline.flood.flood_unique_total, $summary.baseline.accessibility.missing_elevator_station_count)
    Write-Output ("* 대체: " + ($item.station_sequence -join " → "))
    Write-Output ("* 거리 변화: " + $item.comparison.distance_delta)
    Write-Output ("* 시간 raw 변화: " + $item.comparison.req_hr_raw_delta)
    Write-Output ("* 침수흔적: {0} → {1} (-{2})" -f $summary.baseline.flood.flood_unique_total, $item.flood.flood_unique_total, $item.comparison.flood_unique_reduction)
    Write-Output ("* E/V 미확보역: {0} → {1}" -f $summary.baseline.accessibility.missing_elevator_station_count, $item.accessibility.missing_elevator_station_count)
    Write-Output "* MVP 적합 이유: 최단/기본 경로와 다른 API 기반 대체경로이며 침수흔적 노출이 감소하고 엘리베이터 존재 데이터가 악화되지 않는다."
    $rank++
}
if ($safer.Count -eq 0) { Write-Output "유효한 safer alternative 후보가 없습니다." }

Write-Output ""
Write-Output "## 4. 안전한 대체경로가 없는 후보"
foreach ($item in $noSafer) { Write-Output ("* " + $item.origin + " → " + $item.destination + ": " + $item.candidate_status) }

Write-Output ""
Write-Output "## 5. 현재 가장 추천하는 MVP OD"
if ($safer.Count -gt 0) {
    $best = $safer[0]
    $bestSummary = @($comparisonSummaries | Where-Object { $_.origin -eq $best.origin -and $_.destination -eq $best.destination } | Select-Object -First 1)
    Write-Output ("추천: " + $best.origin + " → " + $best.destination + " / 경유 " + $best.through_station)
    Write-Output ("일반 경로 대비 거리 변화: " + $best.comparison.distance_delta)
    Write-Output ("totalReqHr raw 변화: " + $best.comparison.req_hr_raw_delta)
    Write-Output ("다년도 침수흔적 노출 감소: " + $best.comparison.flood_unique_reduction)
    Write-Output ("엘리베이터 미확보역 변화: " + $best.comparison.missing_elevator_delta)
} else {
    Write-Output "추천 가능한 safer alternative OD를 찾지 못했습니다."
}

Write-Output ""
Write-Output "## 6. 다음 단계 제안"
Write-Output "* 후보 OD 중 1개를 고정하고 발표용 시나리오 문장으로 정리"
Write-Output "* 시간대별 강수 demo data를 붙여 Mobility Failure Point 규칙 설계"
Write-Output "* LAD 계산을 raw time 기준으로 분리 구현"
