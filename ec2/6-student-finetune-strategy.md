## Student 모델 평가 및 파인튜닝 전략 (Full vs. LoRA) ##

VLM이 프레임을 이해하려면 ① 이미지에서 특징을 뽑는 비전 인코더(InternViT), ② 이미지·언어 토큰을 같은 공간에 대응시키는 비전-언어 정렬(alignment), ③ 이해한
내용을 문장으로 만드는 언어 생성 능력이 필요하다. 정렬이 특히 중요한데, 이것이 돼 있어야 프레임을 넣었을 때 말이 되는 답이 나온다.

우리 태스크는 프레임 16장을 이어 붙인 "영상"이라 여기에 더해 여러 프레임을 시간 순서로 엮어 이해하는 능력도 필요하다. 하지만 InternVL3-1B는 태생이 이미지
VLM이므로, 이 부분은 zero-shot으로 직접 확인해봐야 한다.

이 zero-shot 측정 결과에 따라 파인튜닝 전략을 정하는데, 기본 능력이 어느 정도 갖춰져 있고 시간적 이해만 보완하면 되는 수준이라면 LoRA로 가볍게 튜닝하고,
정렬이나 시간적 이해가 크게 부족해 모델을 폭넓게 재학습해야 한다면 Full 파인튜닝을 선택한다.

## 모델 테스트 ##
```
# test_student_zeroshot.py
  # [6] 파인튜닝 전 baseline: 베이스 InternVL3-1B 의 zero-shot 영상 이해도 테스트
  # LoRA 어댑터 없이 순수 베이스 모델로 model.chat() 추론.
  # 목적 ① 밑바탕 확인(LoRA로 충분한지 판단)  ② 파인튜닝 후(8번)와 비교할 before 스냅샷
  import argparse, json
  import torch
  import torchvision.transforms as T
  from torchvision.transforms.functional import InterpolationMode
  from PIL import Image
  from transformers import AutoModel, AutoTokenizer

  STUDENT_ID = "OpenGVLab/InternVL3-1B"
  INPUT_SIZE = 448
  IMAGENET_MEAN = (0.485, 0.456, 0.406)
  IMAGENET_STD  = (0.229, 0.224, 0.225)

  # 학습 스크립트와 "동일한" 전처리 (같은 InternVL3 계열 → 하나의 파이프라인)
  _transform = T.Compose([
      T.Lambda(lambda im: im.convert("RGB") if im.mode != "RGB" else im),
      T.Resize((INPUT_SIZE, INPUT_SIZE), interpolation=InterpolationMode.BICUBIC),
      T.ToTensor(),
      T.Normalize(IMAGENET_MEAN, IMAGENET_STD),
  ])

  def load_video_frames(frame_paths):
      """프레임당 타일 1개(max_num=1): [num_frames, 3, 448, 448]."""
      tiles = [_transform(Image.open(p)) for p in frame_paths]
      return torch.stack(tiles)

  def build_video_prompt(question, num_frames):
      """Frame1: <image>\n ... FrameN: <image>\n{question}
      추론에서는 <image> 펼치기를 model.chat 이 자동 처리 (학습 때와 달리 수동 X)."""
      frames = "".join(f"Frame{i+1}: <image>\n" for i in range(num_frames))
      return f"{frames}{question}"

  def main():
      ap = argparse.ArgumentParser()
      ap.add_argument("--data", default="data/train.jsonl",
                      help="teacher answer 를 정답 참고용으로 함께 출력")
      ap.add_argument("--n", type=int, default=5, help="테스트할 샘플 수")
      ap.add_argument("--max-new-tokens", type=int, default=128)
      args = ap.parse_args()

      tok = AutoTokenizer.from_pretrained(STUDENT_ID, trust_remote_code=True)
      model = AutoModel.from_pretrained(
          STUDENT_ID, torch_dtype=torch.bfloat16, trust_remote_code=True
      ).cuda().eval()

      rows = [json.loads(l) for l in open(args.data, encoding="utf-8")][:args.n]
      gen_cfg = dict(max_new_tokens=args.max_new_tokens, do_sample=False)

      for i, r in enumerate(rows):
          pixel_values = load_video_frames(r["frames"]).to(torch.bfloat16).cuda()
          num_frames = pixel_values.shape[0]
          prompt = build_video_prompt(r["question"], num_frames)

          # 비디오 추론: 프레임별 타일 수를 알려줌 (프레임당 1타일 → 전부 1)
          num_patches_list = [1] * num_frames
          with torch.no_grad():
              response = model.chat(
                  tok, pixel_values, prompt, gen_cfg,
                  num_patches_list=num_patches_list,
              )

          print(f"\n=== 샘플 {i} =========================================")
          print(f"Q              : {r['question']}")
          print(f"Student(0-shot): {response}")          # 파인튜닝 전 베이스 모델 답
          print(f"Teacher(정답)  : {r['answer']}")        # 파인튜닝이 좁혀야 할 갭

  if __name__ == "__main__":
      main()
```

아래 스크립트를 실행한다
```
python test_student_zeroshot.py --data data/train.jsonl --n 5
```

#### 출력 예시 (개념) ####
```
=== 샘플 0 =========================================
Q              : 이 영상에서 무슨 일이 일어나는지 설명해줘.
Student(0-shot): 주방처럼 보이는 곳에서 사람이 무언가를 하고 있습니다.        ← 두루뭉술
Teacher(정답)  : 한 남성이 주방에서 재료를 썰어 팬에 볶은 뒤 접시에 담습니다.  ← 구체적
```

이 결과를 어떻게 읽나 (→ 7번 방식 결정)

- 답이 이미 그럴듯 → 밑바탕 튼튼 → 가벼운 LoRA로 충분
- 장면은 대충 맞히나 두루뭉술/디테일 틀림 → 정렬은 되나 부족 → LoRA rank↑ / partial
- 영상 내용을 아예 못 따라옴 → 밑바탕 부족 → full fine-tuning 고려

