"""InternVL3-78B S3 배치 인퍼런스 - 워커 통합본.

사용법:
  python job_infer.py

동작:
  - S3에서 매니페스트/이미지를 읽어 자기 샤드만 InternVL3-78B로 추론
  - 결과를 S3에 주기적으로 업로드 (재시작 시 이어서 처리)

설정은 전부 환경변수로 주입한다.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Set, Tuple

import boto3
from botocore.config import Config as BotoConfig
from PIL import Image
import requests
from vllm import LLM, SamplingParams
from transformers import AutoTokenizer

# ===================== 설정 =====================

def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default

def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default

def _get_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default

def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.lower() in ("1", "true", "yes")

@dataclass
class Config:
    # --- 모델 (InternVL3-78B) ---
    model: str = field(default_factory=lambda: _get_str("MODEL", "/models/InternVL3-78B"))
    tensor_parallel_size: int = field(default_factory=lambda: _get_int("TENSOR_PARALLEL_SIZE", 4))
    max_model_len: int = field(default_factory=lambda: _get_int("MAX_MODEL_LEN", 16384))
    gpu_memory_utilization: float = field(default_factory=lambda: _get_float("GPU_MEMORY_UTILIZATION", 0.92))
    max_images_per_prompt: int = field(default_factory=lambda: _get_int("MAX_IMAGES_PER_PROMPT", 1))
    dtype: str = field(default_factory=lambda: _get_str("DTYPE", "bfloat16"))
    trust_remote_code: bool = field(default_factory=lambda: _get_bool("TRUST_REMOTE_CODE", True))
    max_dynamic_patch: int = field(default_factory=lambda: _get_int("MAX_DYNAMIC_PATCH", 12))

    # --- 샘플링 ---
    temperature: float = field(default_factory=lambda: _get_float("TEMPERATURE", 0.2))
    top_p: float = field(default_factory=lambda: _get_float("TOP_P", 0.9))
    max_tokens: int = field(default_factory=lambda: _get_int("MAX_TOKENS", 1024))
    seed: int = field(default_factory=lambda: _get_int("SEED", 0))

    # --- 샤딩 (K8s: JOB_COMPLETION_INDEX / EC2: SHARD_INDEX 직접 지정) ---
    num_shards: int = field(default_factory=lambda: _get_int("NUM_SHARDS", 1))
    shard_index: int = field(
        default_factory=lambda: _get_int("SHARD_INDEX", _get_int("JOB_COMPLETION_INDEX", 0))
    )

    # --- S3 ---
    s3_bucket: str = field(default_factory=lambda: _get_str("S3_BUCKET", ""))
    input_manifest_key: str = field(default_factory=lambda: _get_str("INPUT_MANIFEST_KEY", "input/manifest.jsonl"))
    image_prefix: str = field(default_factory=lambda: _get_str("IMAGE_PREFIX", "input/images/"))
    output_prefix: str = field(default_factory=lambda: _get_str("OUTPUT_PREFIX", "output/"))
    aws_region: str = field(default_factory=lambda: _get_str("AWS_REGION", "ap-northeast-2"))
    s3_endpoint_url: str = field(default_factory=lambda: _get_str("S3_ENDPOINT_URL", ""))

    # --- 로컬 스크래치 & 업로드 주기 ---
    scratch_dir: str = field(default_factory=lambda: _get_str("SCRATCH_DIR", "/scratch"))
    write_batch_size: int = field(default_factory=lambda: _get_int("WRITE_BATCH_SIZE", 48))
    upload_every: int = field(default_factory=lambda: _get_int("UPLOAD_EVERY", 128))

    # --- 프롬프트 ---
    system_prompt: str = field(default_factory=lambda: _get_str("SYSTEM_PROMPT", ""))
    default_prompt: str = field(
        default_factory=lambda: _get_str("DEFAULT_PROMPT", "이미지를 한국어로 자세히 설명해줘.")
    )

    def validate(self) -> None:
        if not self.s3_bucket:
            raise ValueError("S3_BUCKET 환경변수는 필수입니다.")
        if self.num_shards < 1:
            raise ValueError(f"NUM_SHARDS는 1 이상이어야 함, got {self.num_shards}")
        if not (0 <= self.shard_index < self.num_shards):
            raise ValueError(f"SHARD_INDEX({self.shard_index})는 [0,{self.num_shards}) 범위여야 함")

    def shard_output_key(self) -> str:
        return f"{self.output_prefix.rstrip('/')}/shards/shard-{self.shard_index:05d}.jsonl"

    def summary(self) -> str:
        return (
            f"model={self.model} tp={self.tensor_parallel_size} "
            f"shard={self.shard_index}/{self.num_shards} "
            f"s3://{self.s3_bucket}/{self.input_manifest_key} -> {self.output_prefix}"
        )

# ===================== S3 입출력 =====================

class S3Storage:
    def __init__(self, bucket: str, region: str, endpoint_url: str = "", image_prefix: str = ""):
        self.bucket = bucket
        self.image_prefix = image_prefix
        kwargs: Dict[str, Any] = {
            "region_name": region,
            "config": BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"}),
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self.s3 = boto3.client("s3", **kwargs)

    def iter_manifest(self, key: str) -> Iterator[Dict[str, Any]]:
        """S3의 JSONL을 스트리밍으로 한 줄씩 파싱 (전체 메모리 적재 안 함)."""
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        buf = io.TextIOWrapper(obj["Body"], encoding="utf-8")
        for line in buf:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

    def _parse_s3_uri(self, uri: str) -> Tuple[str, str]:
        rest = uri[len("s3://"):]
        b, _, k = rest.partition("/")
        return b, k

    def load_image(self, spec: str) -> Image.Image:
        """지원: s3://bucket/key, http(s)://, 그 외는 image_prefix 기준 상대 키."""
        if spec.startswith("s3://"):
            b, k = self._parse_s3_uri(spec)
            body = self.s3.get_object(Bucket=b, Key=k)["Body"].read()
            return Image.open(io.BytesIO(body)).convert("RGB")
        if spec.startswith("http://") or spec.startswith("https://"):
            resp = requests.get(spec, timeout=30)
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).convert("RGB")
        key = f"{self.image_prefix.rstrip('/')}/{spec.lstrip('/')}" if self.image_prefix else spec
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return Image.open(io.BytesIO(body)).convert("RGB")

    def download_if_exists(self, key: str, local_path: str) -> bool:
        try:
            self.s3.download_file(self.bucket, key, local_path)
            return True
        except Exception:
            return False

    def upload_file(self, local_path: str, key: str) -> None:
        self.s3.upload_file(local_path, self.bucket, key)

def load_done_ids(local_path: str) -> Set[str]:
    """로컬 샤드 파일에서 이미 처리된 id 집합 복원. 깨진 마지막 줄은 무시."""
    done: Set[str] = set()
    if not os.path.exists(local_path):
        return done
    with open(local_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in obj:
                done.add(str(obj["id"]))
    return done

class ResumableShardWriter:
    """로컬 append + 주기적 S3 업로드."""

    def __init__(self, storage: S3Storage, local_path: str, s3_key: str, upload_every: int):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        self.storage = storage
        self.local_path = local_path
        self.s3_key = s3_key
        self.upload_every = max(1, upload_every)
        self._since_upload = 0
        self._f = open(local_path, "a", encoding="utf-8")

    def write(self, obj: Dict[str, Any]) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._since_upload += 1
        if self._since_upload >= self.upload_every:
            self.upload()

    def upload(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self.storage.upload_file(self.local_path, self.s3_key)
        self._since_upload = 0

    def close(self) -> None:
        try:
            self.upload()
        finally:
            self._f.close()

# ===================== 샤딩 =====================

def iter_shard(records: Iterable[Dict[str, Any]], shard_index: int, num_shards: int) -> Iterator[Dict[str, Any]]:
    """결정적 인터리브 샤딩. 줄번호 % num_shards == shard_index 인 것만 담당."""
    for i, rec in enumerate(records):
        if i % num_shards == shard_index:
            yield rec

# ===================== 추론 =====================

class VLMBatchInferencer:
    """InternVL3-78B vLLM 배치 추론. InternVL은 이미지 자리표시자로 '<image>' 사용."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tokenizer = AutoTokenizer.from_pretrained(cfg.model, trust_remote_code=True)
        self.llm = LLM(
            model=cfg.model,
            tensor_parallel_size=cfg.tensor_parallel_size,   # g7e 4-GPU → 4
            max_model_len=cfg.max_model_len,
            gpu_memory_utilization=cfg.gpu_memory_utilization,
            trust_remote_code=cfg.trust_remote_code,
            dtype=cfg.dtype,
            limit_mm_per_prompt={"image": cfg.max_images_per_prompt},
            mm_processor_kwargs={"max_dynamic_patch": cfg.max_dynamic_patch},
            seed=cfg.seed,
            enforce_eager=False,
        )
        self.sampling = SamplingParams(
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=cfg.max_tokens,
            seed=cfg.seed,
        )

    def _build_prompt(self, prompt: str, num_images: int) -> str:
        if num_images > 1:
            image_tokens = "".join(f"Image-{i+1}: <image>\n" for i in range(num_images))
        elif num_images == 1:
            image_tokens = "<image>\n"
        else:
            image_tokens = ""
        user_text = f"{image_tokens}{prompt}"

        messages = []
        if self.cfg.system_prompt:
            messages.append({"role": "system", "content": self.cfg.system_prompt})
        messages.append({"role": "user", "content": user_text})
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def generate(self, batch: List[Dict[str, Any]]) -> List[str]:
        """batch 항목: {"prompt": str, "images": List[PIL.Image]}"""
        llm_inputs: List[Dict[str, Any]] = []
        for item in batch:
            images: List[Image.Image] = item["images"]
            entry: Dict[str, Any] = {"prompt": self._build_prompt(item["prompt"], len(images))}
            if images:
                entry["multi_modal_data"] = {"image": images if len(images) > 1 else images[0]}
            llm_inputs.append(entry)

        outputs = self.llm.generate(llm_inputs, self.sampling)
        return [o.outputs[0].text.strip() for o in outputs]

# ===================== 워커 메인 =====================

def _log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)

def main() -> int:
    cfg = Config()
    cfg.validate()
    _log(cfg.summary())

    storage = S3Storage(cfg.s3_bucket, cfg.aws_region, cfg.s3_endpoint_url, cfg.image_prefix)

    s3_key = cfg.shard_output_key()
    local_path = os.path.join(cfg.scratch_dir, f"shard-{cfg.shard_index:05d}.jsonl")

    # resume: 기존 샤드 결과를 S3에서 로컬로 내려받기
    if storage.download_if_exists(s3_key, local_path):
        _log(f"기존 샤드 결과 발견 → resume: s3://{cfg.s3_bucket}/{s3_key}")
    done_ids = load_done_ids(local_path)
    _log(f"이미 완료 {len(done_ids)}건 스킵")

    # 이 샤드가 담당하고 아직 안 한 레코드 수집
    pending: List[Dict[str, Any]] = []
    for rec in iter_shard(storage.iter_manifest(cfg.input_manifest_key), cfg.shard_index, cfg.num_shards):
        if str(rec.get("id")) in done_ids:
            continue
        pending.append(rec)
    _log(f"이 샤드 처리 대상: {len(pending)}건")

    if not pending:
        _log("처리할 게 없음. 종료.")
        return 0

    inferencer = VLMBatchInferencer(cfg)  # 78B 로드 (수 분 소요)
    writer = ResumableShardWriter(storage, local_path, s3_key, cfg.upload_every)

    total, processed, start = len(pending), 0, time.time()
    bs = cfg.write_batch_size

    try:
        for i in range(0, total, bs):
            chunk = pending[i:i + bs]
            batch_inputs, meta = [], []
            for rec in chunk:
                rid = str(rec.get("id"))
                prompt = rec.get("prompt") or cfg.default_prompt
                specs = rec.get("images") or ([rec["image"]] if rec.get("image") else [])
                try:
                    images = [storage.load_image(s) for s in specs]
                    batch_inputs.append({"prompt": prompt, "images": images})
                    meta.append({"id": rid, "prompt": prompt, "images": specs, "ok": True})
                except Exception as e:
                    writer.write({"id": rid, "error": f"image_load: {e}", "prompt": prompt})
                    meta.append({"ok": False})

            valid = [b for b, m in zip(batch_inputs, meta) if m.get("ok")]
            valid_meta = [m for m in meta if m.get("ok")]

            if valid:
                try:
                    texts = inferencer.generate(valid)
                    for m, text in zip(valid_meta, texts):
                        writer.write({
                            "id": m["id"], "prompt": m["prompt"], "images": m["images"],
                            "output": text, "model": cfg.model,
                        })
                except Exception as e:
                    _log(f"배치 생성 실패: {e}\n{traceback.format_exc()}")
                    for m in valid_meta:
                        writer.write({"id": m["id"], "error": f"generate: {e}"})

            processed += len(chunk)
            elapsed = time.time() - start
            rate = processed / elapsed if elapsed > 0 else 0.0
            _log(f"진행 {processed}/{total} ({rate:.2f} rec/s) → s3 주기 업로드")
    finally:
        writer.close()  # 마지막 업로드 보장

    _log(f"완료. {processed}건, {time.time() - start:.1f}s → s3://{cfg.s3_bucket}/{s3_key}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
