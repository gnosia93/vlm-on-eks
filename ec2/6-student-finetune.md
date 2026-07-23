

1. 입력이 프레임 리스트 — image 한 개가 아니라 frames(16개 경로) 또는 video 경로 + 샘플링 정보.
2. 프레임당 타일은 1개 — 비디오는 프레임 수가 많아서, InternVL3 관례상 프레임마다 max_num=1(타일링 안 함)로 처리합니다. 안
그러면 16프레임 × 12타일 × 256토큰 → 폭발합니다.
3. 프롬프트가 프레임별로 <image> 반복 — Frame1: <image>\nFrame2: <image>\n... 형태. 각 <image>가 256토큰으로 펼쳐집니다.

### 입력 데이터 포맷 (data/train.jsonl) ###

jsonl 형식으로 한 줄은 하나의 영상 데이터를 의미하며 총 16개의 프레임과 질문(question), 답변(answer) 로 구성되어 있다. 
이 데이터는 티처 모델로 부터 만들어낸 지식 증류 데이터이다. 
```
{"frames": ["data/frames/vid001/f00.jpg", "data/frames/vid001/f01.jpg", "data/frames/vid001/f02.jpg", "...(총 16개)...",
"data/frames/vid001/f15.jpg"], "question": "이 영상에서 무슨 일이 일어나는지 설명해줘.", "answer": "한 남성이 주방에서 재료를
썰어 팬에 볶은 뒤 접시에 담습니다."}
{"frames": ["data/frames/vid002/f00.jpg", "data/frames/vid002/f01.jpg", "...(총 16개)...", "data/frames/vid002/f15.jpg"],
"question": "영상 속 인물이 무엇을 하고 있나요?", "answer": "여성이 공원 벤치에 앉아 노트북으로 작업을 하다가 커피를
마십니다."}
{"frames": ["data/frames/vid003/f00.jpg", "data/frames/vid003/f01.jpg", "...(총 16개)...", "data/frames/vid003/f15.jpg"],
"question": "이 장면의 배경은 어디인가요?", "answer": "해변가로, 파도가 치는 모래사장에서 두 사람이 배구를 하고 있습니다."}
...
```
