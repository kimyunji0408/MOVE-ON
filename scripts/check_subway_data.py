from pathlib import Path
import pandas as pd


SUBWAY_DIR = Path("data/raw/subway")


# subway 폴더의 CSV 파일 찾기
csv_files = list(SUBWAY_DIR.glob("*.csv"))

if not csv_files:
    raise FileNotFoundError(
        "data/raw/subway 폴더에서 CSV 파일을 찾지 못했습니다."
    )


print("=== 발견한 지하철 CSV 파일 ===")

for file in csv_files:
    print(file.name)


# CSV 파일 하나씩 확인
for file in csv_files:

    print("\n" + "=" * 60)
    print("파일명:")
    print(file.name)

    # 서울시 CSV는 cp949인 경우가 많아서
    # 여러 인코딩을 순서대로 시도
    df = None

    for encoding in ["utf-8-sig", "cp949", "euc-kr"]:

        try:
            df = pd.read_csv(
                file,
                encoding=encoding
            )

            print("\n사용한 인코딩:")
            print(encoding)

            break

        except UnicodeDecodeError:
            continue


    if df is None:
        print("CSV를 읽지 못했습니다.")
        continue


    print("\n행 개수:")
    print(len(df))

    print("\n컬럼 목록:")
    print(df.columns.tolist())

    print("\n앞의 5개 데이터:")
    print(df.head())