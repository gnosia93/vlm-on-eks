## zero-shot 영상 이해도 테스트 ##

파인튜닝에는 크게 두 갈래가 있다. LoRA는 기존 가중치를 거의 그대로 두고 작은 어댑터만 얹어 방향을 살짝 트는 방식이고, full fine-tuning은 모델 가중치 전체(또는 상당 부분)를 직접 학습하는 방식이다. 어느 쪽이 맞는지는 베이스 InternVL3-1B가 이미 얼마나 준비돼 있는가에 달려 있다. 

그래서 이 워크샵은 파인튜닝에 들어가기 전에, 먼저 zero-shot 테스트로 밑바탕을 확인한다.

### 무엇을 확인하는가 ###

- 비전 인코더(InternViT) — 이미지에서 의미 있는 특징을 뽑는 능력. 
- 비전-언어 정렬(alignment) — 이미지 토큰과 언어 토큰을 같은 공간에서 대응시키는 능력. 이게 돼 있어야 프레임을 넣었을 때 말이 된다.
- 언어 생성 능력 — 이해한 내용을 문장으로 만들어내는 능력.

여기에 더해, 우리 태스크는 프레임 16장을 이어붙인 "영상" 입력이라 — 여러 프레임을 시간 순서로 엮어 이해하는 능력까지 어느 정도 있어야 한다. 
InternVL3-1B는 태생이 이미지 VLM이므로, 이 부분은 zero-shot으로 직접 확인해봐야 한다.

### zero-shot 결과에 따른 두 갈래 ###

베이스 1B에 프레임을 그냥 넣어보고(zero-shot) 답이 얼마나 그럴듯한지 보면, 어느 방식이 맞는지 감이 온다.

- 밑바탕이 튼튼하면 (그럴듯한 답을 낸다) → 능력은 이미 있고 teacher의 답변 스타일·태스크에만 맞추면 된다. 이 경우 가벼운
LoRA로 효율적으로 증류하는 게 정답이다. 완전히 새로운 능력을 심는 게 아니라, 있는 능력을 특정 방향으로 정렬하는 셈이다.
- 밑바탕이 부족하면 (영상 태스크를 거의 못 한다) → 작은 어댑터로 방향만 트는 걸로는 부족하다. 이때는 LoRA rank를
키우거나(r=64+), 비전-언어 정렬 레이어(MLP projector)까지 학습 대상에 포함하고, 그래도 안 되면 full/partial fine-tuning으로
가중치 자체를 다시 학습해야 한다. (대신 GPU 메모리·학습 비용은 크게 늘어난다)

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

