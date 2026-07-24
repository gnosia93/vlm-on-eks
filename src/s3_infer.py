"""S3에 미리 뽑아둔 프레임(frames/ + frames.json)을 읽어
InternVL3-78B 로 인퍼런스하고, 결과를 다시 S3에 저장하는 예제.

- 입력: s3://$BUCKET/finevideo/sports/<VIDEO_ID>/frames/frames.json 및 프레임 JPG들
- 출력: s3://$BUCKET/finevideo/sports/<VIDEO_ID>/inference/<run_id>.json
- 모델: OpenGVLab/InternVL3-78B, 텐서 병렬 = 노드의 GPU 수
- 실행: BUCKET=my-bucket python s3_infer.py G_VTkkb34gw "이 영상을 한국어로 요약해줘."
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone

import boto3
import torch
from PIL import Image
from transformers import AutoTokenizer
from vllm import LLM, SamplingParams

#MODEL = "OpenGVLab/InternVL3-78B"            
MODEL = "/models/internvl3-78b"            # flat 경로로 부터 읽는다. 
BUCKET = os.environ["BUCKET"]              # 없으면 KeyError로 즉시 중단
DEFAULT_PROMPT = "이 영상의 프레임들을 보고 무슨 일이 일어나는지 한국어로 설명해줘."

s3 = boto3.client("s3")


def _get_object_bytes(key: str) -> bytes:
    """S3 객체를 메모리로 읽어 bytes 반환."""
    resp = s3.get_object(Bucket=BUCKET, Key=key)
    return resp["Body"].read()


def load_frames(video_id: str) -> tuple[dict, list[Image.Image]]:
    """frames.json을 읽고, 거기에 나열된 프레임들을 PIL 이미지 리스트로 로드."""
    manifest_key = f"finevideo/sports/{video_id}/frames/frames.json"
    manifest = json.loads(_get_object_bytes(manifest_key))

    images: list[Image.Image] = []
    for frame_key in manifest["frames"]:          # frames.json의 키는 이미 전체 경로
        raw = _get_object_bytes(frame_key)
        images.append(Image.open(io.BytesIO(raw)).convert("RGB"))

    print(f"[로드 완료] video_id={video_id}, 프레임 {len(images)}장, "
          f"num_frames(명세)={manifest.get('num_frames')}, "
          f"hash={manifest.get('sampling_config_hash')}")
    return manifest, images


def build_prompt(tokenizer, num_frames: int, prompt: str) -> str:
    # 프레임마다 <image> 태그를 붙여 시간 순서를 모델에 알려줌
    frame_tags = "\n".join(f"Frame{i + 1}: <image>" for i in range(num_frames))
    content = f"{frame_tags}\n{prompt}"
    messages = [{"role": "user", "content": content}]
    return tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )


def save_result(video_id: str, manifest: dict, prompt: str,
                answer: str, sampling: SamplingParams) -> str:
    """인퍼런스 결과를 S3에 JSON으로 저장하고 저장 키를 반환."""
    # 프롬프트+설정으로 고유 키를 만들어, 프롬프트 버전별로 결과를 분리 저장
    run_id = hashlib.sha256(
        f"{prompt}|{MODEL}|{manifest.get('sampling_config_hash')}".encode()
    ).hexdigest()[:12]

    result = {
        "video_id": video_id,
        "model": MODEL,
        "prompt": prompt,
        "answer": answer,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sampling_params": {
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "max_tokens": sampling.max_tokens,
        },
        "frames_ref": {
            "num_frames": manifest.get("num_frames"),
            "frame_size": manifest.get("frame_size"),
            "sampling": manifest.get("sampling"),
            "sampling_config_hash": manifest.get("sampling_config_hash"),
        },
    }

    out_key = f"finevideo/sports/{video_id}/inference/{run_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=out_key,
        Body=json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return out_key


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("사용법: BUCKET=my-bucket python s3_infer.py <VIDEO_ID> [프롬프트]")
    video_id = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROMPT

    manifest, images = load_frames(video_id)
    num_frames = len(images)

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)

    num_gpus = torch.cuda.device_count()
    llm = LLM(
        model=MODEL,
        tensor_parallel_size=num_gpus,       # 노드의 모든 GPU 활용
        max_model_len=8192,
        gpu_memory_utilization=0.92,
        dtype="bfloat16",
        trust_remote_code=True,
        limit_mm_per_prompt={"image": num_frames},
    )
    sampling = SamplingParams(temperature=0.2, top_p=0.9, max_tokens=512)

    llm_inputs = [{
        "prompt": build_prompt(tokenizer, num_frames, prompt),
        "multi_modal_data": {"image": images},
    }]

    outputs = llm.generate(llm_inputs, sampling)
    answer = outputs[0].outputs[0].text.strip()

    # 결과를 S3에 저장
    out_key = save_result(video_id, manifest, prompt, answer, sampling)

    print("\n" + "=" * 60)
    print(f"[video_id] {video_id}")
    print(f"[프롬프트] {prompt}")
    print(f"[응답]\n{answer}")
    print("-" * 60)
    print(f"[저장 완료] s3://{BUCKET}/{out_key}")
    print("=" * 60)


if __name__ == "__main__":
    main()
