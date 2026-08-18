from pathlib import Path
import pandas as pd


SUBWAY_DIR = Path("data/raw/subway")


# 파일명에 '역사마스터'가 들어간 CSV 찾기
master_files = list(
    SUBWAY_DIR.glob("*역사마스터*.csv")
)

if not master_files:
    raise FileNotFoundError(
        "역사마스터 CSV 파일을 찾지 못했습니다."
    )


master_file = master_files[0]

print("사용할 파일:")
print(master_file)


# 인코딩 자동 시도
df = None

for encoding in [
    "utf-8-sig",
    "cp949",
    "euc-kr"
]:
    try:
        df = pd.read_csv(
            master_file,
            encoding=encoding
        )

        print("\n사용한 인코딩:")
        print(encoding)

        break

    except UnicodeDecodeError:
        continue


if df is None:
    raise ValueError(
        "CSV 파일을 읽지 못했습니다."
    )


print("\n행 개수:")
print(len(df))

print("\n컬럼 목록:")
print(df.columns.tolist())

print("\n앞의 5개 데이터:")
print(df.head())

print("\n=== 후보역 좌표 확인 ===")

candidate_names = [
    "불광",
    "연신내",
    "독바위",
    "응암",
    "녹번"
]

candidate_stations = df[
    df["역사명"].isin(candidate_names)
][
    [
        "역사_ID",
        "역사명",
        "호선",
        "위도",
        "경도"
    ]
].copy()

print(
    candidate_stations
    .sort_values(["역사명", "호선"])
    .to_string(index=False)
)