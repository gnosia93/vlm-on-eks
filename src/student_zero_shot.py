"""Student 모델(InternVL3-1B)의 zero-shot 영상 이해도 측정.

S3의 frames.json + JPG를 읽어 여러 진단 프롬프트로 추론하고, 결과를 화면에 출력.
(결과를 S3에 저장하지 않음)

- 입력: s3://$BUCKET/finevideo/sports/<VIDEO_ID>/frames/frames.json + 프레임 JPG
- 실행: BUCKET=my-bucket python zeroshot_eval.py <VIDEO_ID>
"""
from __future__ import annotations

import io
import json
import os
import sys

import boto3
import torch
from PIL import Image
from transformers import AutoTokenizer
from vllm import LLM, SamplingParams

# MODEL = "/models/internvl3-78b"          # teacher (대형)
MODEL = "/models/internvl3-1b"             # student: zero-shot 능력 측정 대상
BUCKET = os.environ["BUCKET"]              # 없으면 KeyError로 즉시 중단

# 능력별 진단 프롬프트 — zero-shot으로 무엇이 되고 안 되는지 확인
PROBES = [
    {"key": "caption",  "prompt": "이 프레임들에 무엇이 보이는지 한국어로 설명해줘."},
    {"key": "temporal", "prompt": "프레임 순서대로 장면이 어떻게 변하는지 시간 순서대로 한국어로 설명해줘."},
    {"key": "action",   "prompt": "영상 속 인물이 어떤 동작을 하고 있는지 한국어로 알려줘."},
    {"key": "sport",    "prompt": "이 영상의 스포츠 종목을 한 단어로만 답해줘."},
]

s3 = boto3.client("s3")


def _get_object_bytes(key: str) -> bytes:
    resp = s3.get_object(Bucket=BUCKET, Key=key)
    return resp["Body"].read()


def load_frames(video_id: str) -> tuple[dict, list[Image.Image]]:
    """frames.json을 읽고, 나열된 프레임들을 PIL 이미지로 로드."""
    manifest_key = f"finevideo/sports/{video_id}/frames/frames.json"
    manifest = json.loads(_get_object_bytes(manifest_key))

    images: list[Image.Image] = []
    for frame_key in manifest["frames"]:
        raw = _get_object_bytes(frame_key)
        images.append(Image.open(io.BytesIO(raw)).convert("RGB"))

    print(f"[로드 완료] video_id={video_id}, 프레임 {len(images)}장, "
          f"hash={manifest.get('sampling_config_hash')}")
    return manifest, images


def build_prompt(tokenizer, num_frames: int, prompt: str) -> str:
    frame_tags = "\n".join(f"Frame{i + 1}: <image>" for i in range(num_frames))
    content = f"{frame_tags}\n{prompt}"
    messages = [{"role": "user", "content": content}]
    return tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("사용법: BUCKET=my-bucket python zeroshot_eval.py <VIDEO_ID>")
    video_id = sys.argv[1].strip("/")     # 끝 슬래시 방어

    manifest, images = load_frames(video_id)
    num_frames = len(images)

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)

    num_gpus = torch.cuda.device_count()
    llm = LLM(
        model=MODEL,
        tensor_parallel_size=num_gpus,
        max_model_len=8192,
        gpu_memory_utilization=0.90,
        dtype="bfloat16",
        trust_remote_code=True,
        limit_mm_per_prompt={"image": num_frames},
    )
    sampling = SamplingParams(temperature=0.2, top_p=0.9, max_tokens=512)

    # 모든 진단 프롬프트를 한 번에 배치로 추론 (프레임은 동일하게 재사용)
    llm_inputs = [{
        "prompt": build_prompt(tokenizer, num_frames, p["prompt"]),
        "multi_modal_data": {"image": images},
    } for p in PROBES]

    outputs = llm.generate(llm_inputs, sampling)

    print("\n" + "=" * 60)
    print(f"[video_id] {video_id}  (zero-shot, {MODEL})")
    for probe, out in zip(PROBES, outputs):
        answer = out.outputs[0].text.strip()
        print("-" * 60)
        print(f"[{probe['key']}] {probe['prompt']}")
        print(f"→ {answer}")
    print("=" * 60)


if __name__ == "__main__":
    main()
