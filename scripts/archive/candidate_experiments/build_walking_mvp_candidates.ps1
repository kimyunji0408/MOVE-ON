$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutputDir = Join-Path $ProjectRoot "data/processed/walking_mvp_candidates"

$PedestrianRoutesFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_pedestrian_safe_routes.geojson"
$MobilityFacilitiesFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_mobility_facilities.geojson"
$PedestrianPointsFile = Join-Path $ProjectRoot "data/processed/mobility/eunpyeong_pedestrian_support_points.geojson"
$ValidationReportFile = Join-Path $ProjectRoot "data/processed/mobility/validation_report.json"
$FloodFile = Join-Path $ProjectRoot "data/processed/flood/seoul_flood_trace_2022_2025.geojson"
$StationMasterFile = Join-Path $ProjectRoot "data/raw/subway/서울시 역사마스터 정보.csv"
$StationAddressFile = Join-Path $ProjectRoot "data/raw/subway/서울교통공사_역주소 및 전화번호_20250318.csv"
$AccessibilityFile = Join-Path $ProjectRoot "data/processed/accessibility/candidate_station_accessibility.csv"
$OdCandidatesFile = Join-Path $ProjectRoot "data/processed/od_candidates/eunpyeong_od_route_candidates.json"

$LinestringAnalysisCsv = Join-Path $OutputDir "pedestrian_linestring_analysis.csv"
$FloodIntersectionsCsv = Join-Path $OutputDir "pedestrian_flood_intersections.csv"
$StationLinkedRoutesCsv = Join-Path $OutputDir "station_linked_walking_routes.csv"
$AlternativeComparisonsCsv = Join-Path $OutputDir "walking_alternative_comparisons.csv"
$EndToEndCandidatesJson = Join-Path $OutputDir "end_to_end_mvp_candidates.json"
$TopCandidatesJson = Join-Path $OutputDir "top_end_to_end_mvp_candidates.json"
$ReportJson = Join-Path $OutputDir "walking_mvp_analysis_report.json"

$Years = @(2022, 2023, 2024, 2025)
$StationLinkRadii = @(100, 300, 500)
$PrimaryLinkRadius = 300

function Read-Json($Path) {
    return Get-Content -Raw -Encoding utf8 -LiteralPath $Path | ConvertFrom-Json
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
    throw "Could not read CSV with expected Korean headers: $Path"
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

function Get-FeatureId($Feature, [int]$Index) {
    if (-not [string]::IsNullOrWhiteSpace([string]$Feature.properties.source_id)) { return [string]$Feature.properties.source_id }
    if (-not [string]::IsNullOrWhiteSpace([string]$Feature.properties.SUB_ID)) { return "pedestrian_safe_route-$($Feature.properties.SUB_ID)" }
    return "pedestrian_safe_route-$Index"
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

function Get-LineLengthMeters($Coordinates) {
    if ($Coordinates.Count -lt 2) { return 0.0 }
    $sumLon = 0.0
    $sumLat = 0.0
    foreach ($coord in $Coordinates) {
        $sumLon += [double]$coord[0]
        $sumLat += [double]$coord[1]
    }
    $centerLon = $sumLon / $Coordinates.Count
    $centerLat = $sumLat / $Coordinates.Count
    $length = 0.0
    for ($i = 0; $i -lt $Coordinates.Count - 1; $i++) {
        $a = Project-ToLocalMeters ([double]$Coordinates[$i][0]) ([double]$Coordinates[$i][1]) $centerLon $centerLat
        $b = Project-ToLocalMeters ([double]$Coordinates[$i + 1][0]) ([double]$Coordinates[$i + 1][1]) $centerLon $centerLat
        $dx = $b[0] - $a[0]
        $dy = $b[1] - $a[1]
        $length += [Math]::Sqrt($dx * $dx + $dy * $dy)
    }
    return $length
}

function Get-BBox($Coordinates) {
    $minLon = [double]::PositiveInfinity; $minLat = [double]::PositiveInfinity
    $maxLon = [double]::NegativeInfinity; $maxLat = [double]::NegativeInfinity
    foreach ($coord in $Coordinates) {
        $lon = [double]$coord[0]; $lat = [double]$coord[1]
        if ($lon -lt $minLon) { $minLon = $lon }
        if ($lon -gt $maxLon) { $maxLon = $lon }
        if ($lat -lt $minLat) { $minLat = $lat }
        if ($lat -gt $maxLat) { $maxLat = $lat }
    }
    return [pscustomobject][ordered]@{ min_lon = $minLon; min_lat = $minLat; max_lon = $maxLon; max_lat = $maxLat }
}

function Get-CoordinatesFlat($Coordinates, $Output) {
    if ($Coordinates -is [System.Array] -and $Coordinates.Count -ge 2 -and $Coordinates[0] -is [ValueType]) {
        $Output.Add(@([double]$Coordinates[0], [double]$Coordinates[1])) | Out-Null
        return
    }
    foreach ($item in $Coordinates) { Get-CoordinatesFlat $item $Output }
}

function Get-GeometryBBox($Geometry) {
    $coords = New-Object System.Collections.Generic.List[object]
    Get-CoordinatesFlat $Geometry.coordinates $coords
    return Get-BBox $coords
}

function Test-BBoxMayIntersect($A, $B) {
    return -not ($A.max_lon -lt $B.min_lon -or $A.min_lon -gt $B.max_lon -or $A.max_lat -lt $B.min_lat -or $A.min_lat -gt $B.max_lat)
}

function Convert-RingToMeters($Ring, [double]$CenterLon, [double]$CenterLat) {
    $points = @()
    foreach ($coord in $Ring) {
        $p = Project-ToLocalMeters ([double]$coord[0]) ([double]$coord[1]) $CenterLon $CenterLat
        $points += ,@([double]$p[0], [double]$p[1])
    }
    return $points
}

function Test-PointInRing([double]$X, [double]$Y, $Ring) {
    $inside = $false
    $j = $Ring.Count - 1
    for ($i = 0; $i -lt $Ring.Count; $i++) {
        $xi = [double]$Ring[$i][0]; $yi = [double]$Ring[$i][1]
        $xj = [double]$Ring[$j][0]; $yj = [double]$Ring[$j][1]
        if ((($yi -gt $Y) -ne ($yj -gt $Y)) -and ($X -lt (($xj - $xi) * ($Y - $yi) / ($yj - $yi + 1e-12) + $xi))) {
            $inside = -not $inside
        }
        $j = $i
    }
    return $inside
}

function Test-PointInPolygon([double]$X, [double]$Y, $PolygonRingsMeters) {
    if ($PolygonRingsMeters.Count -eq 0) { return $false }
    if (-not (Test-PointInRing $X $Y $PolygonRingsMeters[0])) { return $false }
    for ($i = 1; $i -lt $PolygonRingsMeters.Count; $i++) {
        if (Test-PointInRing $X $Y $PolygonRingsMeters[$i]) { return $false }
    }
    return $true
}

function Add-SegmentIntersectionT([double]$X1, [double]$Y1, [double]$X2, [double]$Y2, [double]$X3, [double]$Y3, [double]$X4, [double]$Y4, $TValues) {
    $rx = $X2 - $X1; $ry = $Y2 - $Y1
    $sx = $X4 - $X3; $sy = $Y4 - $Y3
    $den = $rx * $sy - $ry * $sx
    if ([Math]::Abs($den) -lt 1e-9) { return }
    $qx = $X3 - $X1; $qy = $Y3 - $Y1
    $t = ($qx * $sy - $qy * $sx) / $den
    $u = ($qx * $ry - $qy * $rx) / $den
    if ($t -ge -1e-9 -and $t -le 1 + 1e-9 -and $u -ge -1e-9 -and $u -le 1 + 1e-9) {
        $bounded = [Math]::Max(0.0, [Math]::Min(1.0, $t))
        $TValues.Add([Math]::Round($bounded, 12)) | Out-Null
    }
}

function Get-LinePolygonOverlapLengthMeters($LineCoordinates, $PolygonCoordinates) {
    $sumLon = 0.0; $sumLat = 0.0; $pointCount = 0
    foreach ($coord in $LineCoordinates) {
        $sumLon += [double]$coord[0]; $sumLat += [double]$coord[1]; $pointCount++
    }
    foreach ($ring in $PolygonCoordinates) {
        foreach ($coord in $ring) {
            $sumLon += [double]$coord[0]; $sumLat += [double]$coord[1]; $pointCount++
        }
    }
    if ($pointCount -eq 0) { return 0.0 }
    $centerLon = $sumLon / $pointCount
    $centerLat = $sumLat / $pointCount
    $rings = @()
    foreach ($ring in $PolygonCoordinates) { $rings += ,(Convert-RingToMeters $ring $centerLon $centerLat) }
    $line = @()
    foreach ($coord in $LineCoordinates) {
        $p = Project-ToLocalMeters ([double]$coord[0]) ([double]$coord[1]) $centerLon $centerLat
        $line += ,@([double]$p[0], [double]$p[1])
    }
    $overlap = 0.0
    for ($i = 0; $i -lt $line.Count - 1; $i++) {
        $x1 = [double]$line[$i][0]; $y1 = [double]$line[$i][1]
        $x2 = [double]$line[$i + 1][0]; $y2 = [double]$line[$i + 1][1]
        $segLen = [Math]::Sqrt((($x2 - $x1) * ($x2 - $x1)) + (($y2 - $y1) * ($y2 - $y1)))
        if ($segLen -le 0) { continue }
        $tValues = New-Object System.Collections.Generic.List[double]
        $tValues.Add(0.0) | Out-Null
        $tValues.Add(1.0) | Out-Null
        foreach ($ring in $rings) {
            for ($r = 0; $r -lt $ring.Count - 1; $r++) {
                Add-SegmentIntersectionT $x1 $y1 $x2 $y2 ([double]$ring[$r][0]) ([double]$ring[$r][1]) ([double]$ring[$r + 1][0]) ([double]$ring[$r + 1][1]) $tValues
            }
        }
        $sortedT = @($tValues | Sort-Object -Unique)
        for ($t = 0; $t -lt $sortedT.Count - 1; $t++) {
            $ta = [double]$sortedT[$t]
            $tb = [double]$sortedT[$t + 1]
            if ($tb -le $ta) { continue }
            $mid = ($ta + $tb) / 2.0
            $mx = $x1 + ($x2 - $x1) * $mid
            $my = $y1 + ($y2 - $y1) * $mid
            if (Test-PointInPolygon $mx $my $rings) {
                $overlap += $segLen * ($tb - $ta)
            }
        }
    }
    return $overlap
}

function Get-LineFloodIntersection($LineCoordinates, $LineBBox, $FloodFeature) {
    $geom = $FloodFeature.geometry
    $featureOverlap = 0.0
    if ($geom.type -eq "Polygon") {
        if (Test-BBoxMayIntersect $LineBBox $FloodFeature.__bbox) {
            $featureOverlap += Get-LinePolygonOverlapLengthMeters $LineCoordinates $geom.coordinates
        }
    } elseif ($geom.type -eq "MultiPolygon") {
        if (Test-BBoxMayIntersect $LineBBox $FloodFeature.__bbox) {
            foreach ($polygon in $geom.coordinates) {
                $featureOverlap += Get-LinePolygonOverlapLengthMeters $LineCoordinates $polygon
            }
        }
    }
    return $featureOverlap
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
        $stations += [pscustomobject][ordered]@{ station = $name; line = Canonical-Line $row.호선; lon = $lon; lat = $lat }
    }
    return @($stations | Sort-Object station, line -Unique)
}

function Build-AccessibilityIndex() {
    $rows = Import-Csv -LiteralPath $AccessibilityFile -Encoding utf8
    $index = @{}
    foreach ($group in ($rows | Group-Object station_name, line_name)) {
        $first = $group.Group[0]
        $key = "$($first.station_name)|$($first.line_name)"
        $index[$key] = [pscustomobject][ordered]@{
            elevator_exists = (@($group.Group | Where-Object { $_.facility_type -eq "elevator" }).Count -gt 0)
            wheelchair_lift_exists = (@($group.Group | Where-Object { $_.facility_type -eq "wheelchair_lift" }).Count -gt 0)
        }
    }
    return $index
}

function Get-RouteAccessibility($Route, $AccessIndex) {
    $missing = 0
    $byStation = @()
    for ($i = 0; $i -lt $Route.station_sequence.Count; $i++) {
        $station = [string]$Route.station_sequence[$i]
        $line = [string]$Route.station_line_sequence[$i]
        $key = "$station|$line"
        $access = if ($AccessIndex.ContainsKey($key)) { $AccessIndex[$key] } else { $null }
        $elevator = if ($null -ne $access) { $access.elevator_exists } else { $null }
        if ($elevator -ne $true) { $missing++ }
        $byStation += [pscustomobject][ordered]@{
            station = $station
            line = $line
            elevator_exists = $elevator
            wheelchair_lift_exists = if ($null -ne $access) { $access.wheelchair_lift_exists } else { $null }
        }
    }
    return [pscustomobject][ordered]@{
        by_station = $byStation
        all_stations_have_elevator = ($missing -eq 0)
        missing_elevator_station_count = $missing
    }
}

function Get-NearbyFeatureCount($Features, $LineCoordinates, [double]$RadiusMeters) {
    $count = 0
    foreach ($feature in $Features) {
        if ($feature.geometry.type -ne "Point") { continue }
        $min = [double]::PositiveInfinity
        foreach ($coord in $LineCoordinates) {
            $d = Get-PointDistanceMeters ([double]$feature.geometry.coordinates[0]) ([double]$feature.geometry.coordinates[1]) ([double]$coord[0]) ([double]$coord[1])
            if ($d -lt $min) { $min = $d }
        }
        if ($min -le $RadiusMeters) { $count++ }
    }
    return $count
}

New-Item -ItemType Directory -Force $OutputDir | Out-Null

$validation = Read-Json $ValidationReportFile
$pedestrian = Read-Json $PedestrianRoutesFile
$mobility = Read-Json $MobilityFacilitiesFile
$pedestrianPoints = Read-Json $PedestrianPointsFile
$flood = Read-Json $FloodFile
$stations = Build-Stations
$accessIndex = Build-AccessibilityIndex
$odCandidates = Read-Json $OdCandidatesFile

$pedestrianFeatures = @($pedestrian.features | Where-Object { $_.geometry.type -eq "LineString" })
$floodFeatures = @($flood.features | Where-Object { ([string]$_.properties.ADM_CD).StartsWith("11380") })
for ($i = 0; $i -lt $floodFeatures.Count; $i++) {
    $floodFeatures[$i] | Add-Member -NotePropertyName "__feature_id" -NotePropertyValue ("flood_$i") -Force
    $floodFeatures[$i] | Add-Member -NotePropertyName "__bbox" -NotePropertyValue (Get-GeometryBBox $floodFeatures[$i].geometry) -Force
}

$lines = @()
for ($i = 0; $i -lt $pedestrianFeatures.Count; $i++) {
    $feature = $pedestrianFeatures[$i]
    $coords = @($feature.geometry.coordinates)
    $lengthFromRaw = To-DoubleOrNull $feature.properties.route_length_m
    $computedLength = Get-LineLengthMeters $coords
    $keptLength = if ($null -ne $lengthFromRaw) { $lengthFromRaw } else { $computedLength }
    $lengthDiffRatio = if ($computedLength -gt 0) { [Math]::Abs($keptLength - $computedLength) / $computedLength } else { 0.0 }
    $lengthForRatio = if ($lengthDiffRatio -gt 0.1) { $computedLength } else { $keptLength }
    $bbox = Get-BBox $coords
    $start = $coords[0]
    $end = $coords[$coords.Count - 1]
    $lines += [pscustomobject][ordered]@{
        route_id = Get-FeatureId $feature $i
        source_id = [string]$feature.properties.source_id
        source_sub_id = [string]$feature.properties.source_sub_id
        destination_name = [string]$feature.properties.destination_name
        address_old = [string]$feature.properties.address_old
        address_new = [string]$feature.properties.address_new
        address = if (-not [string]::IsNullOrWhiteSpace([string]$feature.properties.address_new)) { [string]$feature.properties.address_new } else { [string]$feature.properties.address_old }
        route_length_m = [Math]::Round($keptLength, 3)
        route_length_source = if ($null -ne $lengthFromRaw) { "raw route_length_m" } else { "computed geometry length" }
        length_used_for_ratio_m = [Math]::Round($lengthForRatio, 3)
        length_consistency_status = if ($lengthDiffRatio -gt 0.1) { "WARNING_RAW_LENGTH_DIFFERS_FROM_GEOMETRY" } else { "PASS" }
        computed_length_m = [Math]::Round($computedLength, 3)
        coordinate_count = $coords.Count
        start_lon = [double]$start[0]
        start_lat = [double]$start[1]
        end_lon = [double]$end[0]
        end_lat = [double]$end[1]
        bbox_min_lon = $bbox.min_lon
        bbox_min_lat = $bbox.min_lat
        bbox_max_lon = $bbox.max_lon
        bbox_max_lat = $bbox.max_lat
        slope_difficulty_raw = [string]$feature.properties.slope_difficulty_raw
        slope_risk = [string]$feature.properties.slope_risk
        coordinates = $coords
        bbox = $bbox
    }
}

$linestringCsv = @($lines | ForEach-Object {
    [pscustomobject][ordered]@{
        route_id = $_.route_id
        source_id = $_.source_id
        source_sub_id = $_.source_sub_id
        destination_name = $_.destination_name
        address = $_.address
        route_length_m = $_.route_length_m
        route_length_source = $_.route_length_source
        length_used_for_ratio_m = $_.length_used_for_ratio_m
        length_consistency_status = $_.length_consistency_status
        computed_length_m = $_.computed_length_m
        coordinate_count = $_.coordinate_count
        start_lon = $_.start_lon
        start_lat = $_.start_lat
        end_lon = $_.end_lon
        end_lat = $_.end_lat
        slope_risk = $_.slope_risk
        slope_difficulty_raw = $_.slope_difficulty_raw
    }
})
$linestringCsv | Export-Csv -LiteralPath $LinestringAnalysisCsv -Encoding utf8BOM -NoTypeInformation

$floodRows = @()
$lineFloodIndex = @{}
foreach ($line in $lines) {
    $yearCounts = @{}; $yearLengths = @{}
    foreach ($year in $Years) { $yearCounts[[string]$year] = 0; $yearLengths[[string]$year] = 0.0 }
    $totalCount = 0; $totalLength = 0.0
    foreach ($feature in $floodFeatures) {
        if (-not (Test-BBoxMayIntersect $line.bbox $feature.__bbox)) { continue }
        $overlap = Get-LineFloodIntersection $line.coordinates $line.bbox $feature
        if ($overlap -gt 0.01) {
            $year = [string]$feature.properties.F_YR
            if ($yearCounts.ContainsKey($year)) {
                $yearCounts[$year] += 1
                $yearLengths[$year] += $overlap
            }
            $totalCount += 1
            $totalLength += $overlap
        }
    }
    $ratio = if ([double]$line.length_used_for_ratio_m -gt 0) { [Math]::Round($totalLength / [double]$line.length_used_for_ratio_m, 6) } else { $null }
    $record = [pscustomobject][ordered]@{
        route_id = $line.route_id
        destination_name = $line.destination_name
        address = $line.address
        route_length_m = $line.route_length_m
        length_used_for_ratio_m = $line.length_used_for_ratio_m
        length_consistency_status = $line.length_consistency_status
        flood_intersection_count_2022 = $yearCounts["2022"]
        flood_intersection_count_2023 = $yearCounts["2023"]
        flood_intersection_count_2024 = $yearCounts["2024"]
        flood_intersection_count_2025 = $yearCounts["2025"]
        flood_intersection_count_total = $totalCount
        flood_overlap_length_m_2022 = [Math]::Round($yearLengths["2022"], 3)
        flood_overlap_length_m_2023 = [Math]::Round($yearLengths["2023"], 3)
        flood_overlap_length_m_2024 = [Math]::Round($yearLengths["2024"], 3)
        flood_overlap_length_m_2025 = [Math]::Round($yearLengths["2025"], 3)
        flood_overlap_length_m_total = [Math]::Round($totalLength, 3)
        flood_overlap_ratio = $ratio
    }
    $floodRows += $record
    $lineFloodIndex[$line.route_id] = $record
}
$floodRows | Export-Csv -LiteralPath $FloodIntersectionsCsv -Encoding utf8BOM -NoTypeInformation

$stationLinkRows = @()
foreach ($line in $lines) {
    foreach ($station in $stations) {
        $startDistance = Get-PointDistanceMeters $line.start_lon $line.start_lat $station.lon $station.lat
        $endDistance = Get-PointDistanceMeters $line.end_lon $line.end_lat $station.lon $station.lat
        $geometryDistance = Get-MinDistanceToLineStringMeters $line.coordinates $station.lon $station.lat
        foreach ($radius in $StationLinkRadii) {
            if ($startDistance -le $radius -or $endDistance -le $radius -or $geometryDistance -le $radius) {
                $stationLinkRows += [pscustomobject][ordered]@{
                    route_id = $line.route_id
                    destination_name = $line.destination_name
                    address = $line.address
                    route_length_m = $line.route_length_m
                    station = $station.station
                    line = $station.line
                    radius_m = $radius
                    distance_to_station_m = [Math]::Round($geometryDistance, 3)
                    start_distance_to_station_m = [Math]::Round($startDistance, 3)
                    end_distance_to_station_m = [Math]::Round($endDistance, 3)
                    endpoint_near_station = ($startDistance -le $radius -or $endDistance -le $radius)
                    geometry_near_station = ($geometryDistance -le $radius)
                    station_coordinate_source = "서울시 역사마스터 정보.csv 역사 좌표"
                }
            }
        }
    }
}
$stationLinkRows | Export-Csv -LiteralPath $StationLinkedRoutesCsv -Encoding utf8BOM -NoTypeInformation

$alternativeRows = @()
foreach ($group in ($lines | Group-Object destination_name | Where-Object { $_.Name -and $_.Count -ge 2 })) {
    $groupLines = @($group.Group)
    for ($i = 0; $i -lt $groupLines.Count; $i++) {
        for ($j = $i + 1; $j -lt $groupLines.Count; $j++) {
            $a = $groupLines[$i]; $b = $groupLines[$j]
            $fa = $lineFloodIndex[$a.route_id]; $fb = $lineFloodIndex[$b.route_id]
            $short = if ([double]$a.route_length_m -le [double]$b.route_length_m) { $a } else { $b }
            $long = if ($short.route_id -eq $a.route_id) { $b } else { $a }
            $fShort = $lineFloodIndex[$short.route_id]
            $fLong = $lineFloodIndex[$long.route_id]
            $longerButLessFloodOverlap = ([double]$long.route_length_m -gt [double]$short.route_length_m -and [double]$fLong.flood_overlap_length_m_total -lt [double]$fShort.flood_overlap_length_m_total)
            $alternativeRows += [pscustomobject][ordered]@{
                destination_name = $group.Name
                exact_same_destination = $true
                route_a_id = $a.route_id
                route_a_length_m = $a.route_length_m
                route_a_flood_overlap_length_m_total = $fa.flood_overlap_length_m_total
                route_a_flood_overlap_ratio = $fa.flood_overlap_ratio
                route_b_id = $b.route_id
                route_b_length_m = $b.route_length_m
                route_b_flood_overlap_length_m_total = $fb.flood_overlap_length_m_total
                route_b_flood_overlap_ratio = $fb.flood_overlap_ratio
                shorter_route_id = $short.route_id
                longer_route_id = $long.route_id
                longer_route_has_less_flood_overlap = $longerButLessFloodOverlap
            }
        }
    }
}
if ($alternativeRows.Count -gt 0) {
    $alternativeRows | Export-Csv -LiteralPath $AlternativeComparisonsCsv -Encoding utf8BOM -NoTypeInformation
}

$links300 = @($stationLinkRows | Where-Object { $_.radius_m -eq $PrimaryLinkRadius })
$linksByStation = @{}
foreach ($group in ($links300 | Group-Object station, line)) { $linksByStation[$group.Name] = @($group.Group) }
$lineById = @{}
foreach ($line in $lines) { $lineById[$line.route_id] = $line }

$baselineRoutes = @($odCandidates | Where-Object { $_.route_role -eq "baseline" })
$candidateObjects = @()
foreach ($subway in $baselineRoutes) {
    if ($subway.origin -eq $subway.destination) { continue }
    $originLine = [string]$subway.station_line_sequence[0]
    $destLine = [string]$subway.station_line_sequence[$subway.station_line_sequence.Count - 1]
    $originKey = "$($subway.origin), $originLine"
    $destKey = "$($subway.destination), $destLine"
    $originWalks = if ($linksByStation.ContainsKey($originKey)) { @($linksByStation[$originKey] | Sort-Object distance_to_station_m | Select-Object -First 5) } else { @() }
    $destWalks = if ($linksByStation.ContainsKey($destKey)) { @($linksByStation[$destKey] | Sort-Object distance_to_station_m | Select-Object -First 5) } else { @() }
    if ($originWalks.Count -eq 0 -and $destWalks.Count -eq 0) { continue }
    if ($originWalks.Count -eq 0) { $originWalks = @($null) }
    if ($destWalks.Count -eq 0) { $destWalks = @($null) }
    foreach ($originWalk in $originWalks) {
        foreach ($destWalk in $destWalks) {
            $originLineObj = if ($null -ne $originWalk) { $lineById[$originWalk.route_id] } else { $null }
            $destLineObj = if ($null -ne $destWalk) { $lineById[$destWalk.route_id] } else { $null }
            $originFlood = if ($null -ne $originWalk) { $lineFloodIndex[$originWalk.route_id] } else { $null }
            $destFlood = if ($null -ne $destWalk) { $lineFloodIndex[$destWalk.route_id] } else { $null }
            $walkingLength = 0.0
            $walkingFloodLength = 0.0
            $walkingFloodCount = 0
            if ($originLineObj) {
                $walkingLength += [double]$originLineObj.route_length_m
                $walkingFloodLength += [double]$originFlood.flood_overlap_length_m_total
                $walkingFloodCount += [int]$originFlood.flood_intersection_count_total
            }
            if ($destLineObj) {
                $walkingLength += [double]$destLineObj.route_length_m
                $walkingFloodLength += [double]$destFlood.flood_overlap_length_m_total
                $walkingFloodCount += [int]$destFlood.flood_intersection_count_total
            }
            $accessibility = Get-RouteAccessibility $subway $accessIndex
            $ratio = if ($walkingLength -gt 0) { [Math]::Round($walkingFloodLength / $walkingLength, 6) } else { $null }
            $candidateObjects += [pscustomobject][ordered]@{
                origin_walking_segment = if ($originLineObj) {
                    [pscustomobject][ordered]@{
                        route_id = $originLineObj.route_id
                        destination_name = $originLineObj.destination_name
                        address = $originLineObj.address
                        route_length_m = $originLineObj.route_length_m
                        distance_to_station_m = $originWalk.distance_to_station_m
                        flood_overlap_length_m_total = $originFlood.flood_overlap_length_m_total
                        flood_intersection_count_total = $originFlood.flood_intersection_count_total
                    }
                } else { $null }
                origin_station = [pscustomobject][ordered]@{ station = $subway.origin; line = $originLine }
                subway_route = [pscustomobject][ordered]@{
                    station_sequence = $subway.station_sequence
                    station_line_sequence = $subway.station_line_sequence
                    totalDstc = $subway.total_distance
                    totalReqHr_raw = $subway.totalReqHr_raw
                    transfer_count = $subway.transfer_count
                }
                destination_station = [pscustomobject][ordered]@{ station = $subway.destination; line = $destLine }
                destination_walking_segment = if ($destLineObj) {
                    [pscustomobject][ordered]@{
                        route_id = $destLineObj.route_id
                        destination_name = $destLineObj.destination_name
                        address = $destLineObj.address
                        route_length_m = $destLineObj.route_length_m
                        distance_to_station_m = $destWalk.distance_to_station_m
                        flood_overlap_length_m_total = $destFlood.flood_overlap_length_m_total
                        flood_intersection_count_total = $destFlood.flood_intersection_count_total
                    }
                } else { $null }
                walking_flood_analysis = [pscustomobject][ordered]@{
                    walking_distance_before_subway = if ($originLineObj) { $originLineObj.route_length_m } else { $null }
                    walking_distance_after_subway = if ($destLineObj) { $destLineObj.route_length_m } else { $null }
                    walking_flood_overlap_length_m_total = [Math]::Round($walkingFloodLength, 3)
                    walking_flood_overlap_ratio = $ratio
                    walking_flood_feature_count_total = $walkingFloodCount
                }
                accessibility = $accessibility
                candidate_type = if ($walkingFloodLength -gt 0) { "no-safe-alternative / evacuation scenario candidate" } else { "end-to-end subway-linked walking candidate" }
                mvp_reason = "출발 보행 또는 도착 보행 LineString이 역 300m 이내에서 실제 데이터로 연계되고, 지하철 API 기반 baseline 경로와 접근성 존재 데이터가 결합됨."
            }
        }
    }
}

$candidateObjects | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $EndToEndCandidatesJson -Encoding utf8

$saferWalking = @()
foreach ($row in ($alternativeRows | Where-Object { $_.longer_route_has_less_flood_overlap } | Select-Object -First 5)) {
            $saferWalking += $row
}
$noSafePreferred = @($candidateObjects | Where-Object {
        $_.walking_flood_analysis.walking_flood_overlap_length_m_total -gt 0 -and
        $_.accessibility.all_stations_have_elevator -eq $true
    } |
    Sort-Object @{Expression={$_.walking_flood_analysis.walking_flood_overlap_length_m_total};Descending=$true}, @{Expression={$_.walking_flood_analysis.walking_flood_feature_count_total};Descending=$true} |
    Select-Object)
$noSafeFallback = @($candidateObjects | Where-Object {
        $_.walking_flood_analysis.walking_flood_overlap_length_m_total -gt 0 -and
        $_.accessibility.all_stations_have_elevator -ne $true
    } |
    Sort-Object @{Expression={$_.accessibility.missing_elevator_station_count};Descending=$false}, @{Expression={$_.walking_flood_analysis.walking_flood_overlap_length_m_total};Descending=$true} |
    Select-Object)
$noSafeRaw = @($noSafePreferred + $noSafeFallback)
$seenNoSafeOd = [System.Collections.Generic.HashSet[string]]::new()
$noSafe = @()
foreach ($candidate in $noSafeRaw) {
    $key = "$($candidate.origin_station.station)|$($candidate.origin_station.line)|$($candidate.destination_station.station)|$($candidate.destination_station.line)"
    if ($seenNoSafeOd.Add($key)) {
        $noSafe += $candidate
    }
    if ($noSafe.Count -ge 5) { break }
}

[ordered]@{
    safer_walking_alternative_candidates = $saferWalking
    no_safe_alternative_candidates = $noSafe
} | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $TopCandidatesJson -Encoding utf8

$destinationGroups = @($lines | Group-Object destination_name)
$multiDestinationGroups = @($destinationGroups | Where-Object { $_.Name -and $_.Count -ge 2 })
$stationCounts = @()
foreach ($stationName in @("녹번", "불광", "독바위", "연신내", "응암")) {
    $stationCounts += [pscustomobject][ordered]@{
        station = $stationName
        linked_linestring_count_100m = @($stationLinkRows | Where-Object { $_.station -eq $stationName -and $_.radius_m -eq 100 } | Select-Object route_id -Unique).Count
        linked_linestring_count_300m = @($stationLinkRows | Where-Object { $_.station -eq $stationName -and $_.radius_m -eq 300 } | Select-Object route_id -Unique).Count
        linked_linestring_count_500m = @($stationLinkRows | Where-Object { $_.station -eq $stationName -and $_.radius_m -eq 500 } | Select-Object route_id -Unique).Count
    }
}

$maxOverlap = @($floodRows | Sort-Object @{Expression={$_.flood_overlap_length_m_total};Descending=$true} | Select-Object -First 1)
$minOverlap = @($floodRows | Sort-Object @{Expression={$_.flood_overlap_length_m_total};Descending=$false}, @{Expression={$_.route_length_m};Descending=$true} | Select-Object -First 1)

$report = [ordered]@{
    generated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    phase_gate = [ordered]@{
        validation_encoding = $validation.encoding.status
        validation_eunpyeong_filtering = $validation.eunpyeong_filtering.status
        validation_geometry = $validation.geometry_validity.status
        validation_coordinate_system = $validation.coordinate_system.status
        validation_slope_usability = $validation.slope_usability.status
    }
    linestring_structure = [ordered]@{
        total_linestring_count = $lines.Count
        destination_count = @($destinationGroups | Where-Object { $_.Name }).Count
        destinations_with_multiple_linestrings = $multiDestinationGroups.Count
        likely_meaning = "각 LineString은 destination_name 목적시설에 대한 개별 보행안전경로로 해석 가능함."
        start_end_metadata_exists = $false
        connectable_network = $false
        network_reason = "시작점/종료점 노드 ID나 edge 연결 metadata가 없으므로 395개 LineString을 하나의 보행 route graph로 간주할 수 없음."
        walking_alternative_structure = if ($multiDestinationGroups.Count -gt 0) { "동일 destination_name 복수 LineString 그룹은 존재하지만, 동일 OD의 대체경로인지 여부는 원본 metadata만으로 확정할 수 없음." } else { "동일 destination_name 복수 LineString 그룹 없음." }
    }
    flood_direct_intersection = [ordered]@{
        method = "LineString과 은평구 침수흔적 Polygon의 직접 교차 길이 계산. 거리/길이는 local tangent-plane meter 근사 사용."
        eunpyeong_flood_feature_count = $floodFeatures.Count
        intersecting_linestring_count = @($floodRows | Where-Object { $_.flood_intersection_count_total -gt 0 }).Count
        overlap_length_calculation_possible = $true
        length_ratio_note = "route_length_m 원본과 geometry 계산 길이가 10% 이상 다르면 flood_overlap_ratio에는 geometry 계산 길이를 사용하고 length_consistency_status에 WARNING을 남김."
        max_overlap_route = if ($maxOverlap.Count -gt 0) { $maxOverlap[0] } else { $null }
        min_overlap_route = if ($minOverlap.Count -gt 0) { $minOverlap[0] } else { $null }
        year_totals = [ordered]@{
            "2022" = @($floodRows | Measure-Object flood_intersection_count_2022 -Sum).Sum
            "2023" = @($floodRows | Measure-Object flood_intersection_count_2023 -Sum).Sum
            "2024" = @($floodRows | Measure-Object flood_intersection_count_2024 -Sum).Sum
            "2025" = @($floodRows | Measure-Object flood_intersection_count_2025 -Sum).Sum
        }
    }
    station_linkage = [ordered]@{
        station_coordinate_source = "서울시 역사마스터 정보.csv 역사 좌표. 출입구 좌표는 이번 데이터셋에 없어 사용하지 않음."
        counts = $stationCounts
    }
    walking_alternatives = [ordered]@{
        exact_same_destination_pair_count = $alternativeRows.Count
        longer_but_less_flood_overlap_count = @($alternativeRows | Where-Object { $_.longer_route_has_less_flood_overlap }).Count
        caveat = "동일 destination_name 복수 LineString은 확인하되 동일 출발지-목적지의 route alternative라고 확정하지 않음. 따라서 safer walking alternative는 참고 후보이며 최종 MVP 대체경로로 과장하지 않음."
    }
    end_to_end_mvp = [ordered]@{
        candidate_count = $candidateObjects.Count
        subway_included = $true
        generation_method = "지하철 baseline OD와 역 300m 이내 보행 LineString을 결합. LineString 간 임의 연결 없음."
        top_candidate_groups_file = $TopCandidatesJson.Replace($ProjectRoot + "\", "")
    }
    data_limits = @(
        "보행 LineString에 시작점/종료점 노드 ID가 없어 완전한 보행 경로 탐색 그래프를 구성할 수 없음.",
        "동일 destination_name 복수 LineString이 실제 동일 OD의 대체경로인지 원본 metadata만으로 확정할 수 없음.",
        "slope_difficulty_raw는 설명 문구 성격이므로 Risk Score에 사용하지 않음.",
        "elevator/wheelchair lift는 시설 존재 여부이며 실시간 운행 가능 여부가 아님.",
        "침수흔적 중첩은 과거 공간 노출 지표이며 침수확률이 아님."
    )
    next_steps = @(
        "TOP 후보 중 발표용 Plan B 시나리오 1개를 선택한다.",
        "선택 후보에 시간대별 강수 demo data를 결합하는 규칙을 설계한다.",
        "Risk Score 전 단계로 Mobility Failure Point와 LAD 산출에 필요한 raw feature만 고정한다."
    )
}
$report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $ReportJson -Encoding utf8

Write-Output "## 1. 395개 보행 LineString의 실제 의미"
Write-Output ("* 총 LineString 수: " + $lines.Count)
Write-Output ("* 목적지 수: " + $report.linestring_structure.destination_count)
Write-Output ("* 동일 목적지 복수 경로 존재 여부: " + ($multiDestinationGroups.Count -gt 0) + " / 그룹 " + $multiDestinationGroups.Count + "개")
Write-Output ("* 서로 연결 가능한 네트워크인지: False")
Write-Output ("* 보행 alternative 비교가 가능한 구조인지: 제한적. 동일 destination_name 그룹은 있으나 동일 OD 대체경로로 확정하지 않음.")

Write-Output ""
Write-Output "## 2. 침수흔적 직접 교차 결과"
Write-Output ("* 침수흔적과 실제 교차하는 LineString 수: " + $report.flood_direct_intersection.intersecting_linestring_count)
Write-Output ("* overlap length 계산 가능 여부: True")
if ($maxOverlap.Count -gt 0) {
    Write-Output ("* 최대 overlap 경로: {0} / {1} / {2}m" -f $maxOverlap[0].route_id, $maxOverlap[0].destination_name, $maxOverlap[0].flood_overlap_length_m_total)
}
if ($minOverlap.Count -gt 0) {
    Write-Output ("* 최소 overlap 경로: {0} / {1} / {2}m" -f $minOverlap[0].route_id, $minOverlap[0].destination_name, $minOverlap[0].flood_overlap_length_m_total)
}
Write-Output ("* 연도별 교차 feature 집계: 2022={0}, 2023={1}, 2024={2}, 2025={3}" -f $report.flood_direct_intersection.year_totals["2022"], $report.flood_direct_intersection.year_totals["2023"], $report.flood_direct_intersection.year_totals["2024"], $report.flood_direct_intersection.year_totals["2025"])

Write-Output ""
Write-Output "## 3. 지하철역 연계 결과"
foreach ($item in $stationCounts) {
    Write-Output ("* {0}: 100m={1}, 300m={2}, 500m={3}" -f $item.station, $item.linked_linestring_count_100m, $item.linked_linestring_count_300m, $item.linked_linestring_count_500m)
}

Write-Output ""
Write-Output "## 4. 실제 보행 대체경로 후보"
if (@($alternativeRows | Where-Object { $_.longer_route_has_less_flood_overlap }).Count -gt 0) {
    Write-Output ("같은 destination_name 기준 longer-but-less-overlap 후보 수: " + @($alternativeRows | Where-Object { $_.longer_route_has_less_flood_overlap }).Count)
} else {
    Write-Output "같은 destination_name 기준 '짧은 경로 VS 더 긴 대신 침수흔적 중첩이 적은 경로' 후보는 확인되지 않았습니다."
}

Write-Output ""
Write-Output "## 5. End-to-End MVP 후보 TOP 5"
$rank = 1
foreach ($candidate in $noSafe) {
    Write-Output ("{0}. 출발 보행 → {1}({2}) → 지하철 → {3}({4}) → 도착 보행" -f $rank, $candidate.origin_station.station, $candidate.origin_station.line, $candidate.destination_station.station, $candidate.destination_station.line)
    Write-Output ("* 지하철: {0} / 거리 {1} / totalReqHr_raw {2} / 환승 {3}" -f ($candidate.subway_route.station_sequence -join " → "), $candidate.subway_route.totalDstc, $candidate.subway_route.totalReqHr_raw, $candidate.subway_route.transfer_count)
    Write-Output ("* E/V: all_stations_have_elevator={0}, missing={1}" -f $candidate.accessibility.all_stations_have_elevator, $candidate.accessibility.missing_elevator_station_count)
    Write-Output ("* 보행 침수흔적 overlap: {0}m / feature {1}개" -f $candidate.walking_flood_analysis.walking_flood_overlap_length_m_total, $candidate.walking_flood_analysis.walking_flood_feature_count_total)
    Write-Output ("* 후보 유형: " + $candidate.candidate_type)
    $rank++
}
if ($noSafe.Count -eq 0) { Write-Output "출력 가능한 End-to-End 후보가 없습니다." }

Write-Output ""
Write-Output "## 6. 최종 추천"
if ($saferWalking.Count -gt 0) {
    Write-Output "동일 destination_name 기준 참고용 safer walking alternative 후보는 존재하지만, 동일 OD 대체경로임이 확정되지 않아 최종 추천으로 과장하지 않습니다."
} else {
    Write-Output "실제 safer walking alternative는 확정하지 않습니다. 현재 데이터는 동일 destination_name 복수 LineString이 동일 OD 대체경로인지 보장하지 않아 Plan B는 no-safe-alternative / evacuation scenario candidate가 현실적입니다."
}

Write-Output ""
Write-Output "## 7. 다음 단계"
foreach ($step in $report.next_steps) { Write-Output ("* " + $step) }

Write-Output ""
Write-Output "Saved:"
Write-Output $LinestringAnalysisCsv.Replace($ProjectRoot + "\", "")
Write-Output $FloodIntersectionsCsv.Replace($ProjectRoot + "\", "")
if (Test-Path -LiteralPath $AlternativeComparisonsCsv) { Write-Output $AlternativeComparisonsCsv.Replace($ProjectRoot + "\", "") }
Write-Output $StationLinkedRoutesCsv.Replace($ProjectRoot + "\", "")
Write-Output $EndToEndCandidatesJson.Replace($ProjectRoot + "\", "")
Write-Output $TopCandidatesJson.Replace($ProjectRoot + "\", "")
Write-Output $ReportJson.Replace($ProjectRoot + "\", "")
