import polars as pl
from huggingface_hub import hf_hub_download, HfApi

# 실제 샤드 목록에서 첫 번째 파일을 동적으로 가져오기
api = HfApi()
files = api.list_repo_files("HuggingFaceFV/finevideo", repo_type="dataset")
shards = sorted(f for f in files if f.startswith("data/train-") and f.endswith(".parquet"))

# 첫 샤드만 다운로드 (반환된 캐시 경로를 그대로 사용)
path = hf_hub_download(repo_id="HuggingFaceFV/finevideo",
                       filename=shards[0], repo_type="dataset")
print("shard:", shards[0])

j = pl.col("json").struct

# 미리보기 (직속 필드 + content_metadata 안의 title)
df = (
    pl.scan_parquet(path)
      .select(
          j.field("content_parent_category").alias("parent_cat"),
          j.field("content_fine_category").alias("fine_cat"),
          j.field("content_metadata").struct.field("title").alias("title"),
          j.field("duration_seconds").alias("duration"),
          j.field("resolution").alias("resolution"),
          j.field("youtube_title").alias("yt_title"),
      )
      .head(5)
      .collect()
)
print(df)

# 이 샤드의 상위 카테고리 분포
dist = (
    pl.scan_parquet(path)
      .select(j.field("content_parent_category").alias("cat"))
      .collect()
      .group_by("cat")
      .len()
      .sort("len", descending=True)
)
print(dist)
