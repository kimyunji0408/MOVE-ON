# MOVE:ON API 실응답 확인 기록

## 1. 기상청 초단기예보 API

확인일: 2026-08-13

### 활용 목적
현재 및 향후 약 2시간의 강수 정보를 받아
이동경로의 시간대별 위험도 계산에 활용한다.

### API 호출 결과
- HTTP 상태코드: 200
- resultCode: 00
- resultMsg: NORMAL_SERVICE
- JSON 형식 응답 정상 확인
- 실제 초단기예보 데이터 수신 확인

### 주요 활용 데이터
- RN1: 강수 정보
- PTY: 강수 형태
- fcstDate: 예보 날짜
- fcstTime: 예보 시간
- nx / ny: 기상청 예보 격자 좌표

### 원본 응답 저장
기상청 API의 실제 응답을 JSON 형태로 저장하였다.

저장 위치:
data/raw/weather/

예시:
data/raw/weather/weather_20260813_211511.json

### MOVE:ON용 데이터 전처리
기상청 원본 응답에는 여러 기상요소와 여러 시간대의 정보가 포함되어 있으므로,
MOVE:ON에서 필요한 강수 관련 정보만 별도로 추출하였다.

사용 스크립트:
scripts/process_weather_data.py

주요 전처리 내용:
- RN1, PTY 데이터 추출
- 현재 시간대를 기준으로 현재 시간대 / +1시간 / +2시간 예보 선택
- MOVE:ON에서 사용하기 쉬운 JSON 구조로 변환

전처리 결과:
data/processed/weather/weather_current.json

### 확인 과정에서 발생한 이슈

초기 호출 시 아래 인증 오류가 발생하였다.

SERVICE_KEY_IS_NOT_REGISTERED_ERROR
returnReasonCode: 30

공공데이터포털 자체 테스트에서는 정상 호출되는 것을 확인하였으며,
Python requests 사용 시 인증키 URL 인코딩 문제로 판단하였다.

인증키를 unquote 처리한 후 정상 호출되었으며,
HTTP 200 / NORMAL_SERVICE 응답을 확인하였다.

### 데모 활용 계획
실서비스에서는 기상청 초단기예보 API를 통해 실시간 데이터를 받아 사용한다.

해커톤 데모에서는 발표 당일 실제 날씨와 관계없이
폭우 상황을 재현할 수 있도록 실제 과거 폭우 데이터를 별도로 확보하여,
weather_current.json과 동일한 데이터 구조의 데모용 JSON을 생성할 예정이다.

실시간 데이터와 데모 데이터는 동일한 Risk Score 및 경로 평가 로직에 입력되도록 구성한다.

## 2. 서울교통공사 교통약자 이용정보 API

확인일: 2026-08-14

### API 목적
휠체어 사용자가 지하철역을 이용할 때 필요한 교통약자 시설 정보를 확보해, 이후 MOVE:ON의 경로 이용가능성 판단에 입력 데이터로 사용한다.

이번 단계에서는 엘리베이터와 휠체어리프트 정보를 대상으로 한다. 단, 시설 존재 여부만으로 항상 이용 가능하다고 판단하지 않는다.

### 확인 대상 엔드포인트
- 우선 사용 Base URL: `http://openapi.seoul.go.kr:8088`
- URL 형식: `/{KEY}/json/{SERVICE}/{START_INDEX}/{END_INDEX}/`
- 참고 Base URL: `https://apis.data.go.kr/B553766/wksn`
- 엘리베이터: `getWksnElvtr`
- 휠체어리프트: `getWksnWhcllift`

### API 호출 결과
- 실제 호출 시도: 진행
- 현재 완료 상태: 미완료
- 사유:
  - 현재 작업 환경에서 `python.exe` 실행이 불가함.
  - PowerShell 기반 실제 API 호출을 시도했으나 샌드박스 네트워크 제한으로 `System.Net.WebException` 발생.
  - 외부 API 호출 승인 요청이 거절되어 실제 응답 수신, HTTP 상태, API resultCode 확인은 아직 완료하지 못함.

### 주요 응답 필드 확인 계획
스크립트는 원본 응답의 실제 필드를 보존하고, MOVE:ON에서 사용할 최소 필드로 다음 항목을 정규화한다.

- `station_name`: 역사명
- `station_code`: 역사코드
- `line_name`: 호선
- `facility_type`: `elevator` 또는 `wheelchair_lift`
- `facility_label`: 엘리베이터 또는 휠체어리프트
- `nearby_entrance_no`: 출입구 번호 또는 인접 출입구 정보
- `location`: 상세 위치
- `operation_status`: 운영상태. 원본 응답에 없거나 비어 있으면 운영상태 미확인으로 기록
- `raw`: 원본 행 전체

공식 명세에서 확인한 주요 원본 필드는 다음과 같다.

- `lineNm`: 호선명
- `stnCd`: 역코드
- `stnNm`: 역명
- `vcntEntrcNo`: 근접출입구번호
- `oprtngSitu`: 가동현황
- `fcltNo`: 시설번호
- `fcltNm`: 시설명
- `stnNo`: 역번호
- `dtlPstn`: 상세위치
- `bgngFlrGrndUdgdSe`, `bgngFlr`: 시작층 정보
- `endFlrGrndUdgdSe`, `endFlr`: 종료층 정보

### 저장 위치
실제 API 호출 성공 시 다음 위치에 저장한다.

- 원본 응답: `data/raw/accessibility/accessibility_YYYYMMDD_HHMMSS.json`
- 후보역 processed JSON: `data/processed/accessibility/candidate_station_accessibility.json`
- 후보역 processed CSV: `data/processed/accessibility/candidate_station_accessibility.csv`
- 후보역 요약: `data/processed/accessibility/candidate_station_accessibility_summary.json`
- 응답 필드 목록: `data/processed/accessibility/accessibility_response_fields.json`

사용 스크립트:

- `scripts/check_accessibility_api.py`

### 발생한 오류와 해결방법
- 오류 1: 현재 환경에서 Python 런타임 실행 불가
  - 해결방법: Python이 설치되어 있거나 실행 가능한 환경에서 `python scripts/check_accessibility_api.py` 실행
- 오류 2: 샌드박스 네트워크 제한으로 실제 API 호출 실패
  - 해결방법: 외부 네트워크 호출을 승인한 뒤 스크립트 재실행
- 보안 주의:
  - `.env`의 API 키 값은 출력하지 않는다.
  - API 키가 포함된 전체 요청 URL은 출력하거나 저장하지 않는다.

## 서울교통공사 최단경로이동정보 API

확인일: 2026-08-14

### API 목적
MOVE:ON에서 출발역-도착역 후보 지하철 경로를 확보하기 위한 API다. 이번 단계에서는 최소시간, 최단거리, 최소환승 조건의 경로 구조가 실제로 어떻게 반환되는지 검증한다.

### 공식 공공데이터포털 명세 기준
- 공식 endpoint: `https://apis.data.go.kr/B553766/path2/getShtrmPath2`
- 변경 공지 기준일: 2026-06-05
- 기존 URL/파라미터가 아닌 변경 후 URL과 파라미터를 사용한다.

주요 요청 파라미터:
- `serviceKey`: 공공데이터포털 인증키
- `dataType`: `JSON`
- `dptreStn`: 출발역
- `arvlStn`: 도착역
- `searchDt`: 검색 기준일시, `yyyy-MM-dd HH:mm:ss`
- `searchType`: `duration`, `distance`, `transfer`
- `stationValueType`: `name` 또는 `code`
- `schInclYn`: 시간표 포함 여부. 이번 검증 스크립트는 경로 구조 확인을 위해 `N` 사용

### 검증 스크립트
- `scripts/check_route_api.py`
- `.env`의 `METRO_API_KEY`를 사용한다.
- 키 값과 키가 포함된 전체 요청 URL은 출력하지 않는다.
- 요청 구조는 `serviceKey={KEY}` 형태로만 출력한다.

### 검증 절차
1차 OD는 불광 -> 연신내로 고정한다.

각 `searchType`에 대해 다음을 확인한다.
- HTTP status
- `resultCode` / `resultMsg`
- 총 소요시간
- 총 거리
- 환승 횟수
- 이용 노선
- 지나가는 역 순서
- 환승 구간

세 searchType 결과의 노선 목록과 역 목록이 같으면 중복 후보로 표시한다. searchType 이름만 다르다는 이유로 서로 다른 후보경로로 취급하지 않는다.

불광 -> 연신내에서 서로 다른 후보경로가 2개 이상 확보되지 않으면 녹번, 불광, 독바위, 연신내, 응암 사이의 OD 조합을 추가 탐색한다.

### 저장 위치
실행 성공 시 다음 파일을 생성한다.
- 원본 응답: `data/raw/routes/route_api_YYYYMMDD_HHMMSS.json`
- MOVE:ON 공통 구조: `data/processed/routes/candidate_routes.json`

### 주의
- API가 제공한 경로만 사용한다.
- 임의로 지하철 경로를 생성하지 않는다.
- 이번 단계에서는 교통약자 시설조건, 침수위험, Risk Score를 적용하지 않는다.
- 현재 accessibility API의 `oprtngSitu` 값 `M`/`S`는 의미가 공식적으로 검증되기 전까지 이용가능 여부 판단에 사용하지 않는다.
