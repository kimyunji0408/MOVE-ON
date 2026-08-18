import os
import requests
from urllib.parse import unquote
from dotenv import load_dotenv


# .env 파일 불러오기
load_dotenv()

API_KEY = os.getenv("KMA_API_KEY")

# 공공데이터포털 인증키가 URL 인코딩되어 있을 경우
# Python requests에서 중복 인코딩되지 않도록 한 번 풀어줌
API_KEY = unquote(API_KEY)


# API Key 확인
if not API_KEY:
    raise ValueError(
        ".env 파일에서 KMA_API_KEY를 찾을 수 없습니다."
    )


# 기상청 초단기예보 API
URL = (
    "https://apis.data.go.kr/"
    "1360000/VilageFcstInfoService_2.0/"
    "getUltraSrtFcst"
)


params = {
    "ServiceKey": API_KEY,
    "dataType": "JSON",
    "pageNo": 1,
    "numOfRows": 1000,

    "base_date": "20260813",
    "base_time": "1900",

    "nx": 55,
    "ny": 127,
}


response = requests.get(
    URL,
    params=params,
    timeout=10
)


print("HTTP 상태코드:")
print(response.status_code)

print("\n응답 내용 앞부분:")
print(response.text[:3000])

import json
from datetime import datetime
from pathlib import Path


# JSON 응답 파싱
data = response.json()


# 저장 폴더
output_dir = Path("data/raw/weather")

output_dir.mkdir(
    parents=True,
    exist_ok=True
)


# 파일명에 호출 시간 넣기
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

output_file = output_dir / f"weather_{timestamp}.json"


# JSON 저장
with open(
    output_file,
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        data,
        f,
        ensure_ascii=False,
        indent=2
    )


print("\n=== 기상청 원본 JSON 저장 ===")
print("저장 완료!")
print(output_file)