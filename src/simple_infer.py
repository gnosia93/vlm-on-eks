"""g7e.24xlarge 1대(GPU 4장)에서 InternVL3-78B TP=4 인퍼런스 최소 예제.

- 입력: 코드에서 즉석 생성하는 mock 이미지 (외부 파일 불필요)
- 모델: OpenGVLab/InternVL3-78B, 텐서 병렬 4
- 실행: python simple_infer.py
"""
from __future__ import annotations

from PIL import Image, ImageDraw
from vllm import LLM, SamplingParams
from transformers import AutoTokenizer

MODEL = "OpenGVLab/InternVL3-78B"

def make_mock_images() -> list[tuple[Image.Image, str]]:
    """설명거리가 있는 간단한 mock 이미지 몇 장을 생성."""
    samples: list[tuple[Image.Image, str]] = []

    # 1) 파란 배경에 빨간 원
    img1 = Image.new("RGB", (512, 512), (30, 90, 200))
    d1 = ImageDraw.Draw(img1)
    d1.ellipse([160, 160, 352, 352], fill=(220, 40, 40))
    samples.append((img1, "이 이미지에 무엇이 보이는지 한국어로 설명해줘."))

    # 2) 흰 배경에 초록 사각형 3개
    img2 = Image.new("RGB", (512, 512), (245, 245, 245))
    d2 = ImageDraw.Draw(img2)
    for i in range(3):
        x = 60 + i * 140
        d2.rectangle([x, 200, x + 100, 320], fill=(40, 160, 60))
    samples.append((img2, "도형의 개수와 색을 한국어로 알려줘."))

    # 3) 노란 배경에 검은 삼각형
    img3 = Image.new("RGB", (512, 512), (240, 210, 40))
    d3 = ImageDraw.Draw(img3)
    d3.polygon([(256, 120), (140, 380), (372, 380)], fill=(20, 20, 20))
    samples.append((img3, "이미지를 한 문장으로 한국어로 요약해줘."))

    return samples

def build_prompt(tokenizer, prompt: str) -> str:
    # InternVL 형식: 이미지 자리표시자 '<image>'를 텍스트 앞에 붙임
    messages = [{"role": "user", "content": f"<image>\n{prompt}"}]
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

def main() -> None:
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)

    llm = LLM(
        model=MODEL,
        tensor_parallel_size=4,          # GPU 4장에 모델을 나눠 올림
        max_model_len=8192,
        gpu_memory_utilization=0.92,
        dtype="bfloat16",
        trust_remote_code=True,
        limit_mm_per_prompt={"image": 1},
    )
    sampling = SamplingParams(temperature=0.2, top_p=0.9, max_tokens=256)

    samples = make_mock_images()
    llm_inputs = [
        {
            "prompt": build_prompt(tokenizer, prompt),
            "multi_modal_data": {"image": img},
        }
        for img, prompt in samples
    ]

    outputs = llm.generate(llm_inputs, sampling)

    print("\n" + "=" * 60)
    for i, out in enumerate(outputs):
        print(f"[이미지 {i + 1}] 프롬프트: {samples[i][1]}")
        print(f"응답: {out.outputs[0].text.strip()}")
        print("-" * 60)

if __name__ == "__main__":
    main()
