import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


# 원본 / 가공 데이터 폴더
RAW_DIR = Path("data/raw/weather")
PROCESSED_DIR = Path("data/processed/weather")


# processed/weather 폴더가 없으면 생성
PROCESSED_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# 저장된 weather JSON 중 가장 최근 파일 찾기
weather_files = list(RAW_DIR.glob("weather_*.json"))

if not weather_files:
    raise FileNotFoundError(
        "data/raw/weather 폴더에서 기상 JSON을 찾지 못했습니다."
    )


latest_file = max(
    weather_files,
    key=lambda x: x.stat().st_mtime
)

print("사용할 원본 파일:")
print(latest_file)


# JSON 읽기
with open(
    latest_file,
    "r",
    encoding="utf-8"
) as f:
    data = json.load(f)


# 실제 예보 목록 가져오기
items = data["response"]["body"]["items"]["item"]


# 시간별로 데이터 묶기
forecast_by_time = defaultdict(dict)


for item in items:

    category = item["category"]

    # MOVE:ON에서 우선 사용할 항목만 선택
    if category not in ["RN1", "PTY"]:
        continue

    forecast_key = (
        item["fcstDate"],
        item["fcstTime"]
    )

    forecast_by_time[forecast_key][category] = item["fcstValue"]


# 시간 순으로 정렬
sorted_forecasts = sorted(
    forecast_by_time.items()
)


from datetime import datetime
from zoneinfo import ZoneInfo


# 현재 한국 시간
now = datetime.now(ZoneInfo("Asia/Seoul"))

# 현재 시간을 정각 기준으로 맞춤
current_hour = now.replace(
    minute=0,
    second=0,
    microsecond=0
)

print("\n현재 시각:")
print(now.strftime("%Y-%m-%d %H:%M"))


# 현재 시각 이후의 예보만 선택
future_forecasts = []

for (date, time), values in sorted_forecasts:

    forecast_datetime = datetime.strptime(
        date + time,
        "%Y%m%d%H%M"
    ).replace(
        tzinfo=ZoneInfo("Asia/Seoul")
    )

    if forecast_datetime >= current_hour:
        future_forecasts.append(
            ((date, time), values)
        )


# 가장 가까운 미래 예보부터 3개 시점 선택
selected_forecasts = future_forecasts[:3]

forecast_list = []

for (date, time), values in selected_forecasts:

    forecast_list.append(
        {
            "date": date,
            "time": time,
            "rainfall": values.get("RN1"),
            "precipitation_type": values.get("PTY")
        }
    )


# 원본 응답에서 위치 및 기준시각 가져오기
first_item = items[0]


processed_data = {
    "source": "KMA_ULTRA_SHORT_FORECAST",

    "base_date": first_item["baseDate"],
    "base_time": first_item["baseTime"],

    "location": {
        "nx": first_item["nx"],
        "ny": first_item["ny"]
    },

    "forecast": forecast_list
}


# 저장
OUTPUT_FILE = (
    PROCESSED_DIR
    / "weather_current.json"
)


with open(
    OUTPUT_FILE,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        processed_data,
        f,
        ensure_ascii=False,
        indent=2
    )


print("\n=== MOVE:ON 기상 데이터 생성 완료 ===")
print(OUTPUT_FILE)

print("\n저장 내용:")
print(
    json.dumps(
        processed_data,
        ensure_ascii=False,
        indent=2
    )
)