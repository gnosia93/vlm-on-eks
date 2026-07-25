## Student 모델 파인튜닝 ##

Teacher 모델로는 InternVL3-78B 를 사용하고, student 모델로는 같은 계열의 InternVL3-1B를 사용한다. 
Teacher teacher 라벨링 결과 (인프런스 결과) 기반으로 student 모델은 LoRA 파인 튜닝한다.

### 1. 학습 데이터의 이해  ###

* 학습 데이터 위치
```
$ aws s3 cp s3://vlm-data-499514681453-ap-northeast-2/finevideo/sports/manifest.jsonl - | jq .
{
  "video_id": "NlWPjAq9RXU",
  "category": "Sports",
  "fine_category": "Documentary Profiles",
  "video_key": "finevideo/sports/NlWPjAq9RXU/video.mp4",
  "metadata_key": "finevideo/sports/NlWPjAq9RXU/metadata.json",
  "channel": "Syncpedia",
  "title": "World-famous actor and martial artist Bruce Lee",
  "upload_date": "20240129",
  "duration": 27,
  "resolution": "360x640"
}
{
  "video_id": "QxrYJf48s-4",
  "category": "Sports",
  "fine_category": "Sports Talk Shows",
  "video_key": "finevideo/sports/QxrYJf48s-4/video.mp4",
  "metadata_key": "finevideo/sports/QxrYJf48s-4/metadata.json",
  "channel": "rdres",
  "title": "Fastest 50m Underwater Dolphin Kick Hill Taylor",
  "upload_date": "20111010",
  "duration": 40,
  "resolution": "400x226"
}
...
```

* 각 영상별 프레임 데이터
```
$ aws s3 cp s3://vlm-data-499514681453-ap-northeast-2/finevideo/sports/09buIj5Z5lk/frames/frames.json -
{
  "video_id": "09buIj5Z5lk",
  "num_frames": 16,
  "sampling": "uniform",
  "frame_size": "448x448",
  "source_duration": 62,
  "frames": [
    "finevideo/sports/09buIj5Z5lk/frames/frame_001.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_002.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_003.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_004.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_005.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_006.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_007.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_008.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_009.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_010.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_011.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_012.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_013.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_014.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_015.jpg",
    "finevideo/sports/09buIj5Z5lk/frames/frame_016.jpg"
  ],
  "sampling_config_hash": "d587a6"
}
```

* teacher 라벨링 결과 (인프런스 결과)
```
$ aws s3 cp s3://vlm-data-499514681453-ap-northeast-2/finevideo/sports/09buIj5Z5lk/inference/042dd539417d.json -

{
  "video_id": "09buIj5Z5lk",
  "model": "/models/internvl3-78b",
  "prompt": "이 영상을 한국어로 설명해줘.",
  "answer": "이 영상은 크리켓 경기의 한 장면을 보여줍니다. 파키스탄이 104/1로 경기를 진행 중이며, 목표점은 288점입니다. 투수는 라즈 아흐메드가 등판하고 있습니다. 타자는 공을 치고 뛰기 시작하지만, 수비수들이 빠르게 반응하여 아웃을 성공합니다. 이후 다른 타자가 등장하여 공을 치지만, 또다시 아웃됩니다. 팀원들이 기뻐하며 축하합니다.",
  "run_id": "042dd539417d",
  "created_at": "2026-07-24T21:53:26.067942+00:00",
  "sampling_params": {
    "temperature": 0.2,
    "top_p": 0.9,
    "max_tokens": 512
  },
  "frames_ref": {
    "num_frames": 16,
    "frame_size": "448x448",
    "sampling": "uniform",
    "sampling_config_hash": "d587a6"
  }
```




### 2. 비디오 프레임 전처리 ###

#### _본 워크샵에서는 하나의 프레임을 원본 이미지 크기에 상관없이 448×448 로 리사이즈하고, 이를 하나의 타일로 처리한다 - 프레임당 타일 1개로 처리_ ####
  
InternVL의 비전 인코더는 한 번에 448×448 크기의 정사각형 이미지만 입력받도록 설계되어 있다. 하지만 실제 이미지는
고해상도이거나 가로·세로 비율이 제각각이어서, 이를 그냥 448×448로 억지로 리사이즈하면 세부 정보가 뭉개지거나 왜곡된다.
그래서 InternVL은 입력 이미지의 종횡비에 맞춰 여러 개의 448×448 조각(타일)으로 잘라 각 타일을 개별적으로 인코딩하는 동적
타일링(dynamic tiling) 방식을 쓴다. 이렇게 하면 한 장의 이미지라도 해상도가 높을수록 더 많은 타일로 나뉘어 세부 정보를 살릴
수 있으며, max_num 값으로 최대 타일 수의 상한을 정한다(max_num=1이면 쪼개지 않고 448×448 한 장으로 처리).
  
이때 타일 하나는 비전 인코더를 거쳐 256개의 이미지 토큰으로 변환되어 언어모델 입력에 들어간다. 즉 언어모델이 소비하는
이미지 토큰 수는 타일 수 × 256이 된다. 이미지 한 장이라면 최대 12타일(약 3,072토큰)로 충분히 감당할 수 있다.
그러나 비디오는 프레임이 16장이라 사정이 다르다. 프레임마다 이미지처럼 12타일씩 쪼개면 16 × 12 × 256 = 49,152토큰에 달해,
질문·답변 텍스트를 넣기도 전에 대부분 모델의 컨텍스트 한계와 GPU 메모리를 초과해 버린다. 다행히 비디오는 여러 프레임이
시간축으로 이미 충분한 정보를 담고 있으므로, 프레임 하나하나를 잘게 쪼갤 필요가 없다. 따라서 프레임마다 타일링을 하지
않고(max_num=1) 448×448 한 장씩만 넣어 16 × 1 × 256 = 4,096토큰 수준으로 억제한다.
 
### 3. 전처리 바운더리 ###

#### 3-1. 내가(사용자가) 직접 해야 하는 것 ####

- 프레임 샘플링 — 원본 영상에서 16개 프레임을 균등하게 뽑기
- 이미지 전처리 — 각 프레임을 열어 448×448로 리사이즈·정규화하여 pixel_values 텐서로 만들기 (load_video_frames)
- 프롬프트 조립 — Frame1: `<image>` … Frame16: `<image>` + question 배치
- (학습 시) `<image>` 토큰 펼치기 — 각 `<image>`를 `<img><IMG_CONTEXT>×256</img>`로 확장
- (학습 시) loss 마스킹 — question은 -100으로 가리고 answer만 학습 대상으로
- (학습 시) image_flags 구성 — 어떤 타일이 어떤 샘플 소속인지 표시

단, 이 전처리 코드는 밑바닥부터 만드는 게 아니라 InternVL이 공식 예제로 제공하는 유틸 함수를 가져다 쓰는 것이다.

#### 3-2. 모델(InternVL)이 자동으로 해주는 것 ####

- 타일 → 이미지 토큰 변환 — 448×448 타일을 비전 인코더가 256개 토큰으로 인코딩
- vision 임베딩 주입 — <IMG_CONTEXT> 자리(img_context_token_id)에 이미지 임베딩을 끼워 넣기 (forward 내부)
- (추론 시) <image> 토큰 펼치기 — model.chat()이 자리표시자를 256토큰으로 자동 확장

_픽셀을 토큰으로 바꾸는 인코딩은 모델이 알아서 하지만, "영상 → 프레임 → 텐서" 전처리와 학습용 프롬프트 조립은 내가 명시적으로 해줘야 한다._

### 4. Loss 계산 ###

* answer만 loss 계산                 │ labels에서 프롬프트 구간 -100 마스킹



## 파인 튜닝 ##

Teacher 모델로는 InternVL3-78B를, student 모델로는 같은 계열의 InternVL3-1B를 사용하고, Teacher 모델의 레이블을 받아서
LoRA 파인 튜닝한다. 


## 파인 튜닝하기 ##

### GPU 메모리 ###
1B는 작아서 가중치(bf16 ~2GB)와 옵티마이저(LoRA라 <1GB)는 부담이 없고, 활성값이 병목이다. 이미지 토큰 4,096(16×256)+텍스트로 시퀀스가 ~4.5K에 달해, 배치
크기에 따라 활성값이 선형으로 늘어난다.
  
* 배치 사이즈 1 - 약 8~12 GB
* 배치 사이즈 2 - 약 12~18 GB
* 배치 사이즈 4 - 약 20~28 GB

아래 스크립트로 파인 튜닝한다. 
```
git clone https://github.com/gnosia93/vlm-distillation.git
cd vlm-on-aws/src

python train_student.py --data data/train.jsonl --out out/student-ft --bs 4 --accum 2
```


## 인스턴스 삭제 ##

> [!WARNING]
> GPU 인스턴스는 가용 수량이 제한적이고 시간당 비용도 비싸다.
> 다음 단계로 넘어가기 전에 **반드시 삭제**한다.

ubuntu GPU 인스턴스에서 exit 명령어를 두번 수행하셔 vs-code ec2 인스턴스로 돌아온 다음 (프롬프트는 x86_64 이다), GPU 인스턴스를 삭제한다. 
```
echo -e "\n-------------------------------------"
echo "INSTANCE: [ $INSTANCE ]"
echo "REGION: [ $REGION ]"

aws ec2 terminate-instances --instance-ids $INSTANCE --region $REGION
```



