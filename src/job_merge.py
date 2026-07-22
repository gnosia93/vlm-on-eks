"""S3의 shard-*.jsonl 전부를 취합해 train.jsonl로 S3에 저장 (독립 실행).

사용법:
  python job_merge.py

- id 기준 중복 제거(마지막 우선)
- error 레코드 분리(errors.jsonl)
"""
from __future__ import annotations

import json
import os
import sys
from typing import Iterator

import boto3
from botocore.config import Config as BotoConfig

def _env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default

class S3:
    def __init__(self, bucket: str, region: str, endpoint_url: str = ""):
        self.bucket = bucket
        kwargs = {"region_name": region, "config": BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"})}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self.client = boto3.client("s3", **kwargs)

    def list_keys(self, prefix: str) -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def get_text(self, key: str) -> str:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")

    def put_text(self, text: str, key: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"))

def main() -> int:
    bucket = _env("S3_BUCKET", "")
    if not bucket:
        raise ValueError("S3_BUCKET 환경변수는 필수입니다.")
    region = _env("AWS_REGION", "ap-northeast-2")
    endpoint = _env("S3_ENDPOINT_URL", "")
    output_prefix = _env("OUTPUT_PREFIX", "output/").rstrip("/")

    s3 = S3(bucket, region, endpoint)

    shard_prefix = f"{output_prefix}/shards/"
    train_key = f"{output_prefix}/train.jsonl"
    errors_key = f"{output_prefix}/errors.jsonl"

    seen: dict[str, dict] = {}
    errors: list[dict] = []
    n_files = 0
    for key in s3.list_keys(shard_prefix):
        if not key.endswith(".jsonl"):
            continue
        n_files += 1
        for line in s3.get_text(key).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in obj:
                errors.append(obj)
                continue
            seen[str(obj.get("id"))] = obj  # 마지막 값 우선

    print(f"[merge] 샤드 {n_files}개, 정상 {len(seen)}건, 에러 {len(errors)}건", flush=True)

    train_text = "\n".join(json.dumps(o, ensure_ascii=False) for o in seen.values()) + "\n"
    s3.put_text(train_text, train_key)
    print(f"[merge] -> s3://{bucket}/{train_key}", flush=True)

    if errors:
        err_text = "\n".join(json.dumps(o, ensure_ascii=False) for o in errors) + "\n"
        s3.put_text(err_text, errors_key)
        print(f"[merge] -> s3://{bucket}/{errors_key}", flush=True)

    return 0

if __name__ == "__main__":
    sys.exit(main())
