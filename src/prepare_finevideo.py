import os
import re
import json
import polars as pl
import boto3
from huggingface_hub import hf_hub_download, HfApi

# ---- 샤드 목록 (파일명 규칙에 의존하지 않고 실제 목록을 조회) ----
api = HfApi()
files = api.list_repo_files("HuggingFaceFV/finevideo", repo_type="dataset")
shards = sorted(f for f in files if f.startswith("data/train-") and f.endswith(".parquet"))
shards = shards[:10]                         # 워크샵이므로, 앞 10개 샤드만 다운로드 한다.

# ---- 설정 ----
BUCKET   = os.environ["BUCKET"]              # export BUCKET=your-bucket
REGION   = os.environ.get("REGION", "ap-northeast-2")
CATEGORY = "Sports"                          # 타깃 상위 카테고리
PREFIX   = "finevideo/sports"                # S3 최상위 경로
N_SHARDS = len(shards)
MAX_VIDEOS = None                            # 상한 두려면 정수, 전체면 None

s3 = boto3.client("s3", region_name=REGION)
j = pl.col("json").struct

def s3_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False

def s3_put(key: str, data: bytes, content_type: str):
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)

def safe_id(fname: str, shard: int, idx: int) -> str:
    base = os.path.splitext(os.path.basename(fname or ""))[0]
    base = re.sub(r"[^A-Za-z0-9_-]", "_", base).strip("_")
    return base or f"shard{shard:05d}_row{idx:06d}"

def main():
    total = 0
    for shard_idx, shard_file in enumerate(shards):
        # 샤드 단위 재개: done 마커 있으면 통째로 스킵
        done_key = f"{PREFIX}/_done/shard-{shard_idx:05d}"
        if s3_exists(done_key):
            print(f"[shard {shard_idx:05d}] already done, skip")
            continue

        # 조립하지 않고 실제 파일명(shard_file)을 그대로 사용
        path = hf_hub_download(repo_id="HuggingFaceFV/finevideo",
                               filename=shard_file, repo_type="dataset")

        # Sports 행만 필터해서 필요한 것만 메모리로
        df = (
            pl.scan_parquet(path)
              .filter(j.field("content_parent_category") == CATEGORY)
              .select(
                  pl.col("mp4"),
                  j.field("original_video_filename").alias("fname"),
                  pl.col("json"),
              )
              .collect()
        )

        n = 0
        for idx, row in enumerate(df.iter_rows(named=True)):
            vid = safe_id(row["fname"], shard_idx, idx)
            base = f"{PREFIX}/{vid}"
            vkey, mkey = f"{base}/video.mp4", f"{base}/metadata.json"

            if s3_exists(vkey):        # 개별 재개
                continue

            s3_put(vkey, row["mp4"], "video/mp4")
            meta = json.dumps(row["json"], ensure_ascii=False, default=str).encode("utf-8")
            s3_put(mkey, meta, "application/json")
            n += 1
            total += 1
            if MAX_VIDEOS and total >= MAX_VIDEOS:
                break

        # 샤드 완료 마커
        s3_put(done_key, b"", "text/plain")
        print(f"[shard {shard_idx:05d}/{N_SHARDS}] sports {n}개 업로드 (누적 {total})")

        # 디스크 정리: 처리한 parquet 삭제 (안 하면 EBS 가득 참)
        try:
            os.remove(os.path.realpath(path))
        except OSError:
            pass

        if MAX_VIDEOS and total >= MAX_VIDEOS:
            print("MAX_VIDEOS 도달, 종료")
            break

    # 매니페스트
    manifest = {"category": CATEGORY, "prefix": PREFIX,
                "total_videos": total, "n_shards": N_SHARDS}
    s3_put(f"{PREFIX}/manifest.json",
           json.dumps(manifest, indent=2, ensure_ascii=False).encode(), "application/json")
    print("done:", manifest)

if __name__ == "__main__":
    main()
