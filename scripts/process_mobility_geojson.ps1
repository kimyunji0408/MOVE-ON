$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RawDir = Join-Path $ProjectRoot "data/raw/mobility"
$ProcessedDir = Join-Path $ProjectRoot "data/processed/mobility"
$ReportDir = Join-Path $ProjectRoot "data/processed/mobility"

$FacilityRawFile = Join-Path $RawDir "1750212969170_20260818161023.geojson"
$PedestrianRawFile = Join-Path $RawDir "1694517815685_20260818161308.geojson"
$StationMasterFile = Join-Path $ProjectRoot "data/raw/subway/서울시 역사마스터 정보.csv"

$FacilityOutputFile = Join-Path $ProcessedDir "eunpyeong_mobility_facilities.geojson"
$PedestrianRoutesOutputFile = Join-Path $ProcessedDir "eunpyeong_pedestrian_safe_routes.geojson"
$PedestrianPointsOutputFile = Join-Path $ProcessedDir "eunpyeong_pedestrian_support_points.geojson"
$SummaryOutputFile = Join-Path $ReportDir "mobility_geojson_check_summary.json"

$TargetStationLines = @(
    @{ station = "불광"; line = "3호선" },
    @{ station = "불광"; line = "6호선" },
    @{ station = "독바위"; line = "6호선" },
    @{ station = "연신내"; line = "3호선" },
    @{ station = "연신내"; line = "6호선" }
)
$Distances = @(100, 300, 500)

function Read-GeoJson($Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "GeoJSON file not found: $Path"
    }
    return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
}

function Get-ActualGeometry($Geometry) {
    if ($null -eq $Geometry) {
        return $null
    }
    if ($Geometry.type -eq "GeometryCollection") {
        if ($Geometry.geometries.Count -ne 1) {
            throw "GeometryCollection contains $($Geometry.geometries.Count) geometries; manual review required."
        }
        return $Geometry.geometries[0]
    }
    return $Geometry
}

function Get-NameValueMap($Properties) {
    $map = [ordered]@{}
    for ($i = 1; $i -le 20; $i++) {
        $nameKey = "NAME_{0:D2}" -f $i
        $valueKey = "VALUE_{0:D2}" -f $i
        $name = [string]$Properties.$nameKey
        $value = $Properties.$valueKey
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $map[$name] = $value
        }
    }
    return $map
}

function Get-NameValue($Map, [string[]]$Names) {
    foreach ($name in $Names) {
        if ($Map.Contains($name)) {
            return $Map[$name]
        }
    }
    return $null
}

function To-NullableDouble($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return $null
    }
    $cleaned = ([string]$Value) -replace ",", ""
    $result = 0.0
    if ([double]::TryParse($cleaned, [ref]$result)) {
        return $result
    }
    return $null
}

function Convert-KoreanBoolean($Value) {
    if ($null -eq $Value) {
        return $null
    }
    $text = ([string]$Value).Trim()
    if ($text -match "^설치$|^있음$|^가능$|^Y$|^예$") {
        return $true
    }
    if ($text -match "^미\s*설치$|^없음$|^불가$|^N$|^아니오$") {
        return $false
    }
    return $null
}

function Copy-PropertiesToOrderedMap($Properties) {
    $output = [ordered]@{}
    foreach ($property in $Properties.PSObject.Properties) {
        $output[$property.Name] = $property.Value
    }
    return $output
}

function Get-CoordinateStats($GeoJson) {
    $stats = @{
        MinX = [double]::PositiveInfinity
        MinY = [double]::PositiveInfinity
        MaxX = [double]::NegativeInfinity
        MaxY = [double]::NegativeInfinity
        Count = 0
    }

    function Add-Coordinate($coord, $statsRef) {
        if ($coord -is [System.Array] -and $coord.Count -ge 2 -and $coord[0] -is [ValueType]) {
            $x = [double]$coord[0]
            $y = [double]$coord[1]
            if ($x -lt $statsRef.MinX) { $statsRef.MinX = $x }
            if ($x -gt $statsRef.MaxX) { $statsRef.MaxX = $x }
            if ($y -lt $statsRef.MinY) { $statsRef.MinY = $y }
            if ($y -gt $statsRef.MaxY) { $statsRef.MaxY = $y }
            $statsRef.Count += 1
            return
        }
        foreach ($item in $coord) {
            Add-Coordinate $item $statsRef
        }
    }

    foreach ($feature in $GeoJson.features) {
        $geometry = Get-ActualGeometry $feature.geometry
        if ($null -ne $geometry -and $null -ne $geometry.coordinates) {
            Add-Coordinate $geometry.coordinates $stats
        }
    }

    return $stats
}

function Test-Wgs84Bbox($Stats) {
    return (
        $Stats.Count -gt 0 -and
        $Stats.MinX -ge 124 -and $Stats.MaxX -le 132 -and
        $Stats.MinY -ge 33 -and $Stats.MaxY -le 39
    )
}

function New-FeatureCollection($Features) {
    return [ordered]@{
        type = "FeatureCollection"
        crs = [ordered]@{
            type = "name"
            properties = [ordered]@{
                name = "EPSG:4326"
            }
        }
        features = @($Features)
    }
}

function New-NormalizedFeature($Feature, [int]$Index, [string]$SourceKind) {
    $geometry = Get-ActualGeometry $Feature.geometry
    $props = Copy-PropertiesToOrderedMap $Feature.properties
    $nameValueMap = Get-NameValueMap $Feature.properties

    $lon = To-NullableDouble $Feature.properties.COORD_X
    $lat = To-NullableDouble $Feature.properties.COORD_Y
    $geometryType = if ($null -ne $geometry) { $geometry.type } else { $null }

    $props["source_id"] = "$SourceKind-$Index"
    $props["source_sub_id"] = $Feature.properties.SUB_ID
    $props["name"] = $Feature.properties.CONTENTS_NAME
    $props["address_old"] = $Feature.properties.ADDR_OLD
    $props["address_new"] = $Feature.properties.ADDR_NEW
    $props["lon"] = $lon
    $props["lat"] = $lat
    $props["status"] = $Feature.properties.CONTENTS_STATUS
    $props["geometry_type"] = $geometryType
    $props["name_value_map"] = $nameValueMap

    if ($SourceKind -eq "mobility_facility") {
        $props["facility_type"] = Get-NameValue $nameValueMap @("편의시설 구분", "시설구분")
        $props["slope_degree"] = To-NullableDouble (Get-NameValue $nameValueMap @("경사도"))
        $props["ramp_available"] = Convert-KoreanBoolean (Get-NameValue $nameValueMap @("경사로 설치 여부", "이동식 경사로 설치 여부"))
        $props["automatic_door"] = Convert-KoreanBoolean (Get-NameValue $nameValueMap @("자동문 설치 여부"))
    }

    if ($SourceKind -eq "pedestrian_safe_route" -or $SourceKind -eq "pedestrian_support_point") {
        $props["destination_name"] = $Feature.properties.CONTENTS_NAME
        $props["route_length_m"] = To-NullableDouble (Get-NameValue $nameValueMap @("경로 길이(m)"))
        $props["slope_difficulty_raw"] = Get-NameValue $nameValueMap @("보행 경로의 앞,뒤 경사 난이도")
        $props["slope_risk"] = "unknown"
    }

    return [ordered]@{
        type = "Feature"
        properties = $props
        geometry = $geometry
    }
}

function Test-EunpyeongAddress($Properties) {
    $newAddress = [string]$Properties.ADDR_NEW
    $oldAddress = [string]$Properties.ADDR_OLD
    return ($newAddress.Contains("서울특별시 은평구") -or $oldAddress.Contains("서울특별시 은평구") -or $newAddress.Contains("은평구") -or $oldAddress.Contains("은평구"))
}

function Test-HasRouteLength($Properties) {
    for ($i = 1; $i -le 20; $i++) {
        $nameKey = "NAME_{0:D2}" -f $i
        $name = [string]$Properties.$nameKey
        if ($name -eq "경로 길이(m)" -or $name.Contains("경로 길이")) {
            return $true
        }
    }
    return $false
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
    if ($text -match "(\d+)") {
        return "$([int]$Matches[1])호선"
    }
    return $text
}

function Get-StationPoints() {
    $rows = Read-StationMaster
    $stations = @()
    foreach ($row in $rows) {
        $lon = To-NullableDouble $row."경도"
        $lat = To-NullableDouble $row."위도"
        if ($null -eq $lon -or $null -eq $lat) {
            continue
        }
        $stationName = $row."역사명"
        $lineName = Convert-LineName $row."호선"
        $isTarget = $false
        foreach ($target in $TargetStationLines) {
            if ($target.station -eq $stationName -and $target.line -eq $lineName) {
                $isTarget = $true
                break
            }
        }
        if (-not $isTarget) {
            continue
        }
        $stations += [ordered]@{
            station = $stationName
            line = $lineName
            lon = $lon
            lat = $lat
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
    $px = 0.0
    $py = 0.0
    $abx = $b[0] - $a[0]
    $aby = $b[1] - $a[1]
    $apx = $px - $a[0]
    $apy = $py - $a[1]
    $denom = $abx * $abx + $aby * $aby
    if ($denom -eq 0) {
        return [Math]::Sqrt($apx * $apx + $apy * $apy)
    }
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
        if ($distance -lt $minDistance) {
            $minDistance = $distance
        }
    }
    return $minDistance
}

function Get-ProximityStats($Stations, $FacilityFeatures, $RouteFeatures) {
    $stats = @()
    foreach ($station in $Stations) {
        foreach ($distance in $Distances) {
            $facilityCount = 0
            foreach ($feature in $FacilityFeatures) {
                $lon = $feature.properties.lon
                $lat = $feature.properties.lat
                if ($null -eq $lon -or $null -eq $lat) { continue }
                if ((Get-PointDistanceMeters $lon $lat $station.lon $station.lat) -le $distance) {
                    $facilityCount += 1
                }
            }

            $routeCount = 0
            foreach ($feature in $RouteFeatures) {
                if ($feature.geometry.type -ne "LineString") { continue }
                $minDistance = Get-MinDistanceToLineStringMeters $feature.geometry.coordinates $station.lon $station.lat
                if ($minDistance -le $distance) {
                    $routeCount += 1
                }
            }

            $stats += [ordered]@{
                station = $station.station
                line = $station.line
                radius_m = $distance
                mobility_facility_count = $facilityCount
                pedestrian_safe_route_linestring_count = $routeCount
            }
        }
    }
    return $stats
}

function Get-GeometryTypeCounts($Features) {
    $counts = [ordered]@{
        Point = 0
        LineString = 0
        Polygon = 0
        Other = 0
    }
    foreach ($feature in $Features) {
        $type = $feature.geometry.type
        if ($counts.Contains($type)) {
            $counts[$type] += 1
        } else {
            $counts.Other += 1
        }
    }
    return $counts
}

function Get-MissingCoordinateCount($Features) {
    $count = 0
    foreach ($feature in $Features) {
        if ($null -eq $feature.properties.lon -or $null -eq $feature.properties.lat) {
            $count += 1
        }
    }
    return $count
}

New-Item -ItemType Directory -Force $ProcessedDir | Out-Null

$facilityRaw = Read-GeoJson $FacilityRawFile
$pedestrianRaw = Read-GeoJson $PedestrianRawFile

$facilityStats = Get-CoordinateStats $facilityRaw
$pedestrianStats = Get-CoordinateStats $pedestrianRaw

$facilityFeatures = @()
for ($i = 0; $i -lt $facilityRaw.features.Count; $i++) {
    $facilityFeatures += New-NormalizedFeature $facilityRaw.features[$i] ($i + 1) "mobility_facility"
}

$eunpyeongFeatures = @()
$pedestrianRouteFeatures = @()
$pedestrianSupportPointFeatures = @()
for ($i = 0; $i -lt $pedestrianRaw.features.Count; $i++) {
    $feature = $pedestrianRaw.features[$i]
    if (-not (Test-EunpyeongAddress $feature.properties)) {
        continue
    }
    $normalized = New-NormalizedFeature $feature ($i + 1) "pedestrian_support_point"
    $eunpyeongFeatures += $normalized
    if ($normalized.geometry.type -eq "LineString" -and (Test-HasRouteLength $feature.properties)) {
        $pedestrianRouteFeatures += New-NormalizedFeature $feature ($i + 1) "pedestrian_safe_route"
    } elseif ($normalized.geometry.type -eq "Point") {
        $pedestrianSupportPointFeatures += $normalized
    }
}

$stationPoints = Get-StationPoints
$proximityStats = Get-ProximityStats $stationPoints $facilityFeatures $pedestrianRouteFeatures
$eunpyeongStats = Get-CoordinateStats ([pscustomobject]@{ features = $eunpyeongFeatures })
$pedestrianRouteStats = Get-CoordinateStats ([pscustomobject]@{ features = $pedestrianRouteFeatures })

$facilityCollection = New-FeatureCollection $facilityFeatures
$routeCollection = New-FeatureCollection $pedestrianRouteFeatures
$pointCollection = New-FeatureCollection $pedestrianSupportPointFeatures

$facilityCollection | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $FacilityOutputFile -Encoding utf8
$routeCollection | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $PedestrianRoutesOutputFile -Encoding utf8
$pointCollection | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $PedestrianPointsOutputFile -Encoding utf8

$facilityTypeCounts = Get-GeometryTypeCounts $facilityFeatures
$eunpyeongTypeCounts = Get-GeometryTypeCounts $eunpyeongFeatures
$routeTypeCounts = Get-GeometryTypeCounts $pedestrianRouteFeatures

$summary = [ordered]@{
    input_files = [ordered]@{
        mobility_facilities = $FacilityRawFile.Replace($ProjectRoot + "\", "")
        pedestrian_safe_routes_source = $PedestrianRawFile.Replace($ProjectRoot + "\", "")
    }
    outputs = [ordered]@{
        mobility_facilities = $FacilityOutputFile.Replace($ProjectRoot + "\", "")
        pedestrian_safe_routes = $PedestrianRoutesOutputFile.Replace($ProjectRoot + "\", "")
        pedestrian_support_points = $PedestrianPointsOutputFile.Replace($ProjectRoot + "\", "")
    }
    mobility_facilities = [ordered]@{
        original_feature_count = $facilityRaw.features.Count
        processed_feature_count = $facilityFeatures.Count
        geometry_counts = $facilityTypeCounts
        missing_coordinate_count = Get-MissingCoordinateCount $facilityFeatures
        bbox = @($facilityStats.MinX, $facilityStats.MinY, $facilityStats.MaxX, $facilityStats.MaxY)
        appears_wgs84 = Test-Wgs84Bbox $facilityStats
    }
    pedestrian_source = [ordered]@{
        original_feature_count = $pedestrianRaw.features.Count
        eunpyeong_address_filter_count = $eunpyeongFeatures.Count
        eunpyeong_geometry_counts = $eunpyeongTypeCounts
        pedestrian_safe_route_linestring_count = $pedestrianRouteFeatures.Count
        pedestrian_safe_route_geometry_counts = $routeTypeCounts
        support_point_count = $pedestrianSupportPointFeatures.Count
        missing_coordinate_count = Get-MissingCoordinateCount $eunpyeongFeatures
        source_bbox = @($pedestrianStats.MinX, $pedestrianStats.MinY, $pedestrianStats.MaxX, $pedestrianStats.MaxY)
        eunpyeong_bbox = @($eunpyeongStats.MinX, $eunpyeongStats.MinY, $eunpyeongStats.MaxX, $eunpyeongStats.MaxY)
        pedestrian_safe_route_bbox = @($pedestrianRouteStats.MinX, $pedestrianRouteStats.MinY, $pedestrianRouteStats.MaxX, $pedestrianRouteStats.MaxY)
        appears_wgs84 = Test-Wgs84Bbox $eunpyeongStats
        filter_method = "ADDR_NEW or ADDR_OLD contains 은평구; no Eunpyeong boundary GeoJSON was found in the project."
    }
    normalized_properties = [ordered]@{
        mobility_facilities = @("source_id", "source_sub_id", "name", "address_old", "address_new", "lon", "lat", "status", "geometry_type", "facility_type", "slope_degree", "ramp_available", "automatic_door", "name_value_map")
        pedestrian_safe_routes = @("source_id", "source_sub_id", "destination_name", "address_old", "address_new", "route_length_m", "lon", "lat", "geometry_type", "slope_difficulty_raw", "slope_risk", "name_value_map")
    }
    slope_note = "보행 경로의 앞,뒤 경사 난이도는 feature별 실제 경사값이 아니라 범례/설명 문자열로 확인되어 slope_risk=unknown으로 보존했다."
    station_proximity_counts = $proximityStats
}

$summary | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $SummaryOutputFile -Encoding utf8

# Re-read generated GeoJSON to verify JSON validity.
[void](Get-Content -Raw -LiteralPath $FacilityOutputFile | ConvertFrom-Json)
[void](Get-Content -Raw -LiteralPath $PedestrianRoutesOutputFile | ConvertFrom-Json)
[void](Get-Content -Raw -LiteralPath $PedestrianPointsOutputFile | ConvertFrom-Json)

Write-Output "=== Mobility facilities ==="
Write-Output ("original features: " + $facilityRaw.features.Count)
Write-Output ("processed features: " + $facilityFeatures.Count)
Write-Output ("Point: " + $facilityTypeCounts.Point + ", LineString: " + $facilityTypeCounts.LineString + ", Polygon: " + $facilityTypeCounts.Polygon)
Write-Output ("missing coordinates: " + (Get-MissingCoordinateCount $facilityFeatures))
Write-Output ("appears WGS84: " + (Test-Wgs84Bbox $facilityStats))

Write-Output ""
Write-Output "=== Pedestrian safe routes source ==="
Write-Output ("original features: " + $pedestrianRaw.features.Count)
Write-Output ("Eunpyeong filtered features: " + $eunpyeongFeatures.Count)
Write-Output ("Eunpyeong Point: " + $eunpyeongTypeCounts.Point + ", LineString: " + $eunpyeongTypeCounts.LineString)
Write-Output ("safe route LineString: " + $pedestrianRouteFeatures.Count)
Write-Output ("missing coordinates: " + (Get-MissingCoordinateCount $eunpyeongFeatures))
Write-Output ("appears WGS84: " + (Test-Wgs84Bbox $pedestrianStats))

Write-Output ""
Write-Output "=== Station proximity counts ==="
foreach ($row in $proximityStats) {
    Write-Output ("{0}({1}) {2}m: facilities={3}, safe_route_lines={4}" -f $row.station, $row.line, $row.radius_m, $row.mobility_facility_count, $row.pedestrian_safe_route_linestring_count)
}

Write-Output ""
Write-Output "Saved:"
Write-Output ($FacilityOutputFile.Replace($ProjectRoot + "\", ""))
Write-Output ($PedestrianRoutesOutputFile.Replace($ProjectRoot + "\", ""))
Write-Output ($PedestrianPointsOutputFile.Replace($ProjectRoot + "\", ""))
Write-Output ($SummaryOutputFile.Replace($ProjectRoot + "\", ""))
