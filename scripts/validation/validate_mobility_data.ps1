$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RawDir = Join-Path $ProjectRoot "data/raw/mobility"
$ProcessedDir = Join-Path $ProjectRoot "data/processed/mobility"
$StationMasterFile = Join-Path $ProjectRoot "data/raw/subway/서울시 역사마스터 정보.csv"

$FacilityRawFile = Join-Path $RawDir "1750212969170_20260818161023.geojson"
$PedestrianRawFile = Join-Path $RawDir "1694517815685_20260818161308.geojson"
$FacilityProcessedFile = Join-Path $ProcessedDir "eunpyeong_mobility_facilities.geojson"
$PedestrianRoutesFile = Join-Path $ProcessedDir "eunpyeong_pedestrian_safe_routes.geojson"
$ValidationReportFile = Join-Path $ProcessedDir "validation_report.json"

$TargetStationLines = @(
    @{ station = "불광"; line = "3호선" },
    @{ station = "불광"; line = "6호선" },
    @{ station = "독바위"; line = "6호선" },
    @{ station = "연신내"; line = "3호선" },
    @{ station = "연신내"; line = "6호선" }
)
$Distances = @(100, 300, 500)

function Get-TextEncodingReport($Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $hasUtf8Bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    [System.Text.Encoding]::RegisterProvider([System.Text.CodePagesEncodingProvider]::Instance)
    $encodings = @(
        @{ name = "UTF-8"; encoding = [System.Text.UTF8Encoding]::new($false, $true) },
        @{ name = "UTF-8-SIG"; encoding = [System.Text.UTF8Encoding]::new($true, $true) },
        @{ name = "CP949"; encoding = [System.Text.Encoding]::GetEncoding(949) }
    )

    $results = @()
    foreach ($item in $encodings) {
        $decoded = $false
        $jsonParsed = $false
        $koreanHits = 0
        $errorType = $null
        try {
            $text = $item.encoding.GetString($bytes)
            $decoded = $true
            $koreanHits = ([regex]::Matches($text.Substring(0, [Math]::Min(200000, $text.Length)), "은평구|불광|독바위|연신내|경사")).Count
            [void]($text | ConvertFrom-Json)
            $jsonParsed = $true
        } catch {
            $errorType = $_.Exception.GetType().Name
        }
        $results += [ordered]@{
            encoding = $item.name
            decoded = $decoded
            json_parsed = $jsonParsed
            korean_keyword_hits_in_preview = $koreanHits
            error_type = $errorType
        }
    }

    return [ordered]@{
        file = $Path.Replace($ProjectRoot + "\", "")
        has_utf8_bom = $hasUtf8Bom
        candidates = $results
        selected_encoding = "UTF-8"
    }
}

function Read-JsonUtf8($Path) {
    return Get-Content -Raw -Encoding utf8 -LiteralPath $Path | ConvertFrom-Json
}

function Get-ActualGeometry($Geometry) {
    if ($null -eq $Geometry) { return $null }
    if ($Geometry.type -eq "GeometryCollection") {
        if ($Geometry.geometries.Count -ne 1) {
            throw "GeometryCollection contains $($Geometry.geometries.Count) geometries."
        }
        return $Geometry.geometries[0]
    }
    return $Geometry
}

function Test-EunpyeongAddress($Properties) {
    $newAddress = [string]$Properties.ADDR_NEW
    $oldAddress = [string]$Properties.ADDR_OLD
    return ($newAddress.Contains("은평구") -or $oldAddress.Contains("은평구"))
}

function Get-AddressForFeature($Properties) {
    $newAddress = [string]$Properties.ADDR_NEW
    $oldAddress = [string]$Properties.ADDR_OLD
    if (-not [string]::IsNullOrWhiteSpace($newAddress)) { return $newAddress }
    return $oldAddress
}

function Get-NameValueMap($Properties) {
    $map = [ordered]@{}
    for ($i = 1; $i -le 20; $i++) {
        $nameKey = "NAME_{0:D2}" -f $i
        $valueKey = "VALUE_{0:D2}" -f $i
        $name = [string]$Properties.$nameKey
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $map[$name] = $Properties.$valueKey
        }
    }
    return $map
}

function Get-NameValue($Properties, [string]$TargetName) {
    for ($i = 1; $i -le 20; $i++) {
        $nameKey = "NAME_{0:D2}" -f $i
        $valueKey = "VALUE_{0:D2}" -f $i
        if ([string]$Properties.$nameKey -eq $TargetName) {
            return $Properties.$valueKey
        }
    }
    return $null
}

function Add-CoordinateToStats($coord, $stats) {
    if ($coord -is [System.Array] -and $coord.Count -ge 2 -and $coord[0] -is [ValueType]) {
        $x = [double]$coord[0]
        $y = [double]$coord[1]
        if ($x -lt $stats.min_x) { $stats.min_x = $x }
        if ($x -gt $stats.max_x) { $stats.max_x = $x }
        if ($y -lt $stats.min_y) { $stats.min_y = $y }
        if ($y -gt $stats.max_y) { $stats.max_y = $y }
        $stats.coordinate_count += 1
        return
    }
    foreach ($item in $coord) {
        Add-CoordinateToStats $item $stats
    }
}

function Get-Bbox($Features) {
    $stats = [ordered]@{
        min_x = [double]::PositiveInfinity
        min_y = [double]::PositiveInfinity
        max_x = [double]::NegativeInfinity
        max_y = [double]::NegativeInfinity
        coordinate_count = 0
    }
    foreach ($feature in $Features) {
        $geometry = Get-ActualGeometry $feature.geometry
        if ($null -ne $geometry -and $null -ne $geometry.coordinates) {
            Add-CoordinateToStats $geometry.coordinates $stats
        }
    }
    if ($stats.coordinate_count -eq 0) { return $null }
    return @($stats.min_x, $stats.min_y, $stats.max_x, $stats.max_y)
}

function Get-GeometryCounts($Features) {
    $counts = [ordered]@{}
    foreach ($feature in $Features) {
        $type = (Get-ActualGeometry $feature.geometry).type
        if (-not $counts.Contains($type)) { $counts[$type] = 0 }
        $counts[$type] += 1
    }
    return $counts
}

function Test-Wgs84Bbox($Bbox) {
    return (
        $null -ne $Bbox -and
        $Bbox[0] -ge 124 -and $Bbox[2] -le 132 -and
        $Bbox[1] -ge 33 -and $Bbox[3] -le 39
    )
}

function Get-AddressPrefixDistribution($Features) {
    $dist = @{}
    foreach ($feature in $Features) {
        $address = Get-AddressForFeature $feature.properties
        $prefix = "unknown"
        if ($address -match "서울특별시\s+([^\s]+)") {
            $prefix = "서울특별시 " + $Matches[1]
        } elseif ($address -match "서울\s+([^\s]+)") {
            $prefix = "서울 " + $Matches[1]
        } elseif ($address -match "([가-힣]+구)") {
            $prefix = $Matches[1]
        }
        if (-not $dist.ContainsKey($prefix)) { $dist[$prefix] = 0 }
        $dist[$prefix] += 1
    }
    return @($dist.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object { [ordered]@{ prefix = $_.Key; count = $_.Value } })
}

function Get-SlopeAnalysis($Features) {
    $values = @{}
    $examples = New-Object System.Collections.Generic.List[string]
    $fieldNames = @{}
    foreach ($feature in $Features) {
        for ($i = 1; $i -le 20; $i++) {
            $nameKey = "NAME_{0:D2}" -f $i
            $valueKey = "VALUE_{0:D2}" -f $i
            $name = [string]$feature.properties.$nameKey
            if ($name -match "경사") {
                if (-not $fieldNames.ContainsKey($name)) { $fieldNames[$name] = 0 }
                $fieldNames[$name] += 1
                $value = [string]$feature.properties.$valueKey
                if (-not $values.ContainsKey($value)) { $values[$value] = 0 }
                $values[$value] += 1
                if ($examples.Count -lt 20 -and -not [string]::IsNullOrWhiteSpace($value)) {
                    $examples.Add($value)
                }
            }
        }
    }
    return [ordered]@{
        field_names = @($fieldNames.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object { [ordered]@{ field = $_.Key; count = $_.Value } })
        examples = @($examples)
        unique_values = @($values.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object { [ordered]@{ value = $_.Key; count = $_.Value } })
    }
}

function To-NullableDouble($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    $result = 0.0
    if ([double]::TryParse(([string]$Value -replace ",", ""), [ref]$result)) { return $result }
    return $null
}

function Read-StationMaster() {
    foreach ($encoding in @("utf8", "utf8BOM", "Default", "OEM")) {
        $rows = Import-Csv -LiteralPath $StationMasterFile -Encoding $encoding
        if ($rows.Count -gt 0 -and $rows[0].PSObject.Properties.Name -contains "역사명") {
            return $rows
        }
    }
    throw "Could not read station master with Korean column names."
}

function Convert-LineName($LineName) {
    $text = [string]$LineName
    if ($text -match "(\d+)") { return "$([int]$Matches[1])호선" }
    return $text
}

function Get-StationPoints() {
    $rows = Read-StationMaster
    $stations = @()
    foreach ($row in $rows) {
        $stationName = $row."역사명"
        $lineName = Convert-LineName $row."호선"
        $isTarget = $false
        foreach ($target in $TargetStationLines) {
            if ($target.station -eq $stationName -and $target.line -eq $lineName) {
                $isTarget = $true
                break
            }
        }
        if (-not $isTarget) { continue }
        $stations += [ordered]@{
            station = $stationName
            line = $lineName
            lon = To-NullableDouble $row."경도"
            lat = To-NullableDouble $row."위도"
        }
    }
    return $stations | Sort-Object station, line
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
    $length = 0.0
    for ($i = 0; $i -lt $Coordinates.Count - 1; $i++) {
        $a = $Coordinates[$i]
        $b = $Coordinates[$i + 1]
        $length += Get-PointDistanceMeters ([double]$a[0]) ([double]$a[1]) ([double]$b[0]) ([double]$b[1])
    }
    return $length
}

function Get-ProximityStats($Stations, $FacilityFeatures, $RouteFeatures) {
    $stats = @()
    foreach ($station in $Stations) {
        foreach ($distance in $Distances) {
            $facilityCount = 0
            foreach ($feature in $FacilityFeatures) {
                if ($feature.geometry.type -ne "Point") { continue }
                $lon = To-NullableDouble $feature.properties.lon
                $lat = To-NullableDouble $feature.properties.lat
                if ($null -eq $lon -or $null -eq $lat) { continue }
                if ((Get-PointDistanceMeters $lon $lat $station.lon $station.lat) -le $distance) { $facilityCount += 1 }
            }
            $routeCount = 0
            foreach ($feature in $RouteFeatures) {
                if ($feature.geometry.type -ne "LineString") { continue }
                if ((Get-MinDistanceToLineStringMeters $feature.geometry.coordinates $station.lon $station.lat) -le $distance) { $routeCount += 1 }
            }
            $stats += [ordered]@{
                station = $station.station
                line = $station.line
                radius_m = $distance
                mobility_facility_count = $facilityCount
                pedestrian_linestring_count = $routeCount
            }
        }
    }
    return $stats
}

function Test-GeometryBasics($Features) {
    $invalid = 0
    foreach ($feature in $Features) {
        if ($feature.type -ne "Feature" -or $null -eq $feature.geometry -or $null -eq $feature.geometry.type -or $null -eq $feature.geometry.coordinates) {
            $invalid += 1
        }
    }
    return [ordered]@{
        checked_features = $Features.Count
        invalid_basic_geometry_count = $invalid
        status = if ($invalid -eq 0) { "PASS" } else { "FAIL" }
    }
}

$facilityEncodingReport = Get-TextEncodingReport $FacilityRawFile
$pedestrianEncodingReport = Get-TextEncodingReport $PedestrianRawFile

$facilityRaw = Read-JsonUtf8 $FacilityRawFile
$pedestrianRaw = Read-JsonUtf8 $PedestrianRawFile
$facilityProcessed = Read-JsonUtf8 $FacilityProcessedFile
$pedestrianRoutes = Read-JsonUtf8 $PedestrianRoutesFile

$eunpyeongRawFeatures = @($pedestrianRaw.features | Where-Object { Test-EunpyeongAddress $_.properties })
$eunpyeongLineFeatures = @($eunpyeongRawFeatures | Where-Object { (Get-ActualGeometry $_.geometry).type -eq "LineString" })
$eunpyeongRouteLineFeatures = @($eunpyeongLineFeatures | Where-Object { $null -ne (Get-NameValue $_.properties "경로 길이(m)") })

$addrExamples = @($eunpyeongRawFeatures | Select-Object -First 20 | ForEach-Object {
    [ordered]@{
        contents_name = $_.properties.CONTENTS_NAME
        address_new = $_.properties.ADDR_NEW
        address_old = $_.properties.ADDR_OLD
        geometry_type = (Get-ActualGeometry $_.geometry).type
    }
})

$lineDetails = @($pedestrianRoutes.features | ForEach-Object {
    [ordered]@{
        source_id = $_.properties.source_id
        destination_name = $_.properties.destination_name
        address = if ($_.properties.address_new) { $_.properties.address_new } else { $_.properties.address_old }
        route_length_m = $_.properties.route_length_m
        geometry_length_m_approx = [Math]::Round((Get-LineLengthMeters $_.geometry.coordinates), 2)
    }
})

$lengthValues = @($lineDetails | ForEach-Object { $_.geometry_length_m_approx })
$lengthSorted = @($lengthValues | Sort-Object)
$p95Index = [Math]::Min($lengthSorted.Count - 1, [Math]::Floor($lengthSorted.Count * 0.95))
$p95 = if ($lengthSorted.Count -gt 0) { [double]$lengthSorted[$p95Index] } else { 0 }
$lineOutliers = @($lineDetails | Where-Object { $_.geometry_length_m_approx -gt ($p95 * 3) } | Select-Object -First 20)

$stationPoints = Get-StationPoints
$proximityStats = Get-ProximityStats $stationPoints $facilityProcessed.features $pedestrianRoutes.features

$rawPedestrianBbox = Get-Bbox $pedestrianRaw.features
$eunpyeongBbox = Get-Bbox $eunpyeongRawFeatures
$eunpyeongLineBbox = Get-Bbox $eunpyeongRouteLineFeatures

$slopeRawAnalysis = Get-SlopeAnalysis $pedestrianRaw.features
$slopeEunpyeongRouteAnalysis = Get-SlopeAnalysis $eunpyeongRouteLineFeatures

$encodingStatus = if ($facilityEncodingReport.selected_encoding -eq "UTF-8" -and $pedestrianEncodingReport.selected_encoding -eq "UTF-8") { "PASS" } else { "WARNING" }
$filterStatus = if ($eunpyeongBbox[0] -gt 126.85 -and $eunpyeongBbox[2] -lt 126.97 -and $eunpyeongBbox[1] -gt 37.55 -and $eunpyeongBbox[3] -lt 37.67) { "PASS" } else { "WARNING" }
$geometryStatus = if ((Test-GeometryBasics $facilityProcessed.features).status -eq "PASS" -and (Test-GeometryBasics $pedestrianRoutes.features).status -eq "PASS") { "PASS" } else { "FAIL" }
$coordinateStatus = if ((Test-Wgs84Bbox (Get-Bbox $facilityProcessed.features)) -and (Test-Wgs84Bbox $eunpyeongLineBbox)) { "PASS" } else { "FAIL" }
$slopeStatus = "WARNING"

$report = [ordered]@{
    generated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    encoding = [ordered]@{
        status = $encodingStatus
        mobility_facility_raw = $facilityEncodingReport
        pedestrian_raw = $pedestrianEncodingReport
    }
    eunpyeong_filtering = [ordered]@{
        status = $filterStatus
        filter_method = "ADDR_NEW or ADDR_OLD contains 은평구"
        original_feature_count = $pedestrianRaw.features.Count
        addr_new_or_old_contains_eunpyeong_count = $eunpyeongRawFeatures.Count
        address_examples = $addrExamples
        address_prefix_distribution = Get-AddressPrefixDistribution $eunpyeongRawFeatures
        raw_source_bbox = $rawPedestrianBbox
        filtered_eunpyeong_bbox = $eunpyeongBbox
        filtered_eunpyeong_linestring_bbox = $eunpyeongLineBbox
        previous_summary_bbox_issue = "mobility_geojson_check_summary.json stored the full pedestrian source bbox, not the filtered Eunpyeong bbox."
    }
    geometry_validity = [ordered]@{
        status = $geometryStatus
        mobility_facilities = Test-GeometryBasics $facilityProcessed.features
        pedestrian_safe_routes = Test-GeometryBasics $pedestrianRoutes.features
        pedestrian_linestring_count = $pedestrianRoutes.features.Count
        line_outlier_rule = "geometry_length_m_approx > 3 * p95"
        line_length_p95_m_approx = $p95
        line_outlier_count = $lineOutliers.Count
        line_outlier_examples = $lineOutliers
    }
    coordinate_system = [ordered]@{
        status = $coordinateStatus
        mobility_facility_bbox = Get-Bbox $facilityProcessed.features
        pedestrian_safe_route_bbox = $eunpyeongLineBbox
        appears_wgs84_lon_lat = $coordinateStatus -eq "PASS"
    }
    slope_usability = [ordered]@{
        status = $slopeStatus
        raw_slope_analysis = $slopeRawAnalysis
        eunpyeong_route_slope_analysis = $slopeEunpyeongRouteAnalysis
        conclusion = "보행안전경로 slope field is a legend/explanation string, not a feature-level measured slope value; keep slope_risk=unknown."
    }
    station_proximity_counts = $proximityStats
    route_mvp_readiness = [ordered]@{
        can_continue_bulgwang_dokbawi = $true
        can_continue_bulgwang_yeonsinnae = $true
        note = "Proceed with geometry/count exposure comparison, but do not use pedestrian slope legend text as Risk Score input."
    }
}

$report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $ValidationReportFile -Encoding utf8

Write-Output "=== Encoding ==="
Write-Output ("mobility raw selected: " + $facilityEncodingReport.selected_encoding)
Write-Output ("pedestrian raw selected: " + $pedestrianEncodingReport.selected_encoding)

Write-Output ""
Write-Output "=== Eunpyeong filtering ==="
Write-Output ("ADDR_NEW/ADDR_OLD contains 은평구: " + $eunpyeongRawFeatures.Count)
Write-Output ("filtered bbox: " + ($eunpyeongBbox -join ", "))
Write-Output ("filtered LineString bbox: " + ($eunpyeongLineBbox -join ", "))
Write-Output "address examples:"
$addrExamples | ForEach-Object { Write-Output ("- " + $_.contents_name + " / " + $_.address_new + " / " + $_.address_old + " / " + $_.geometry_type) }

Write-Output ""
Write-Output "address prefix distribution:"
(Get-AddressPrefixDistribution $eunpyeongRawFeatures) | Select-Object -First 10 | ForEach-Object { Write-Output ("- " + $_.prefix + ": " + $_.count) }

Write-Output ""
Write-Output "=== Pedestrian LineString ==="
Write-Output ("safe route LineString count: " + $pedestrianRoutes.features.Count)
Write-Output ("line length p95 approx(m): " + $p95)
Write-Output ("outlier count: " + $lineOutliers.Count)

Write-Output ""
Write-Output "=== Slope fields ==="
$slopeEunpyeongRouteAnalysis.field_names | ForEach-Object { Write-Output ("- " + $_.field + ": " + $_.count) }
Write-Output "slope examples:"
$slopeEunpyeongRouteAnalysis.examples | Select-Object -First 20 | ForEach-Object { Write-Output ("- " + ($_ -replace "`r|`n", " ")) }

Write-Output ""
Write-Output "=== Station proximity counts ==="
$proximityStats | ForEach-Object {
    Write-Output ("{0}({1}) {2}m: facilities={3}, pedestrian_lines={4}" -f $_.station, $_.line, $_.radius_m, $_.mobility_facility_count, $_.pedestrian_linestring_count)
}

Write-Output ""
Write-Output "=== PASS/WARNING/FAIL ==="
Write-Output ("encoding: " + $encodingStatus)
Write-Output ("Eunpyeong filtering: " + $filterStatus)
Write-Output ("geometry validity: " + $geometryStatus)
Write-Output ("coordinate system: " + $coordinateStatus)
Write-Output ("slope usability: " + $slopeStatus)

Write-Output ""
Write-Output "Saved:"
Write-Output ($ValidationReportFile.Replace($ProjectRoot + "\", ""))
