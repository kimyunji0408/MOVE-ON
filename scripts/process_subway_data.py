from pathlib import Path
import pandas as pd


SUBWAY_DIR = Path("data/raw/subway")

ADDRESS_FILE = (
    SUBWAY_DIR
    / "서울교통공사_역주소 및 전화번호_20250318.csv"
)


# CSV 읽기
df = pd.read_csv(
    ADDRESS_FILE,
    encoding="cp949"
)


# 우리가 현재 보고 있는 후보 자치구
candidate_districts = [
    "은평구",
    "강서구",
    "서대문구"
]


# 도로명주소에서 후보 자치구에 해당하는 역만 찾기
candidate_stations = df[
    df["도로명주소"].str.contains(
        "|".join(candidate_districts),
        na=False
    )
].copy()


# 필요한 컬럼만 보기
candidate_stations = candidate_stations[
    [
        "역번호",
        "호선",
        "역명",
        "도로명주소",
        "지번주소"
    ]
]


print("\n=== 후보지역 지하철역 ===")

print(
    candidate_stations
    .sort_values(["도로명주소", "호선"])
    .to_string(index=False)
)


print("\n총 역 데이터 수:")
print(len(candidate_stations))