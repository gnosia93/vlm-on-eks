## 영상 프레임 샘플링 ##

이 단계의 핵심은 뒤에 오는 InternVL3-78B 인퍼런스가 바로 사용할 수 있는 형태로 프레임을 뽑아 S3에 저장하는 것입니다. 특히 여기서 추출하는 프레임 수가 곧 VLM의 토큰 수와 비용을 결정하기 때문에, 샘플링 전략을 어떻게 세우느냐가 가장 중요합니다.

프레임 샘플링은 영상을 디코딩해서 이미지를 추출하는 작업으로, 전형적인 CPU 바운드 작업입니다. 따라서 이 작업은 GPU 없이 CPU만으로 충분히 처리할 수 있어, x86 대비 비용이 저렴한 Graviton(ARM) 노드에 배치하는 것이 유리합니다. 이렇게 하면 GPU 노드는 프레임 샘플링에 자원을 쓰지 않고 인퍼런스에만 집중할 수 있습니다. 프레임 추출 도구로는 영상 디코딩과 프레임 추출의 사실상 표준인 ffmpeg를 사용하며, ffmpeg는 ARM 아키텍처에서도 안정적으로 동작하기 때문에 Graviton 환경에 잘 맞습니다.

### 1. 샘플링 전략 ###

이 단계에서 가장 중요한 설계 포인트는 프레임 수가 곧 토큰 수를 결정한다는 점입니다. KV 캐시의 크기를 좌우하는 "영상 토큰 폭발"이 바로 이 프레임 샘플링 단계에서 결정되기 때문입니다.
예를 들어 FineVideo는 영상 한 편이 평균 4.7분, 약 282초에 달합니다. 만약 이를 1fps로 추출하면 영상 하나당 282프레임이 나오는데, InternVL3에서 프레임당 약 256토큰(하나의 타일로만 매핑)이 생성되므로 영상 하나가 약 7만 토큰에 이르게 됩니다. 이렇게 되면 시퀀스 하나가 컨텍스트를 거의 다 차지해 버려, 배치 처리가 사실상 불가능해집니다.

그래서 영상 길이와 무관하게 정해진 개수의 프레임만 균일한 간격으로 추출하는 "고정 개수 균일 샘플링(uniform sampling)"을 권장합니다. 이는 토큰 수를 예측 가능한 수준으로 고정해 줄 뿐 아니라, InternVL 계열이 영상을 처리할 때 사용하는 표준 방식이기도 합니다.

| 전략 | 프레임 수 | 대략 토큰 수 | 용도 |
| :--- | :---: | :---: | :--- |
| **저해상 프리뷰** | 8 | ~2,000 | 빠른 프롬프트 튜닝|
| **기본 권장** | 16 | ~4,000 | 대부분의 라벨링 |
| **정밀 분석** | 32 | ~8,000 | 상호작용·내러티브 세밀하게 |

* 영상 길이에 상관없이 N개로 고정하면 토큰 수가 예측 가능해져 GPU 배치 사이징이 안정.
* 균일 샘플링 = 영상을 N구간으로 나눠 각 구간에서 1프레임씩. 장면 전환을 고루 커버.

#### 고정 개수 균일 샘플링 (권장) — 영상 길이 무관하게 16프레임 ####
```
DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 input.mp4)
N=16
FPS=$(echo "scale=6; $N / $DURATION" | bc)

ffmpeg -i input.mp4 \
  -vf "fps=${FPS},scale=448:448:force_original_aspect_ratio=decrease,pad=448:448:(ow-iw)/2:(oh-ih)/2" \
  -frames:v $N \
  -q:v 2 \
  frame_%03d.jpg
```
* scale=448:448 → InternVL3의 기본 입력 타일 크기에 맞춤 (프레임당 토큰 수를 예측 가능하게)
* pad → 종횡비 유지하며 정사각형으로 (왜곡 방지)
* -q:v 2 → JPEG 고품질

#### S3 저장 레이아웃 ####
아래 영상 샘플링 스크립트(sample_frames.sh)를 수행한 후 S3 버킷의 특정 영상 디렉토리의 구조는 아래와 같습니다.
frames 디렉토리가 새롭게 생성되며, 해당 디렉토리안에 이미지들과 frames.json 이 생성됩니다. 
```
s3://$BUCKET/finevideo/sports/G_VTkkb34gw/
├── video.mp4              # 원본 (기존)
├── metadata.json          # 메타/전사 (기존)
└── frames/                # ← 새로 생성
    ├── frame_001.jpg
    ├── frame_002.jpg
    ├── ...
    └── frames.json        # 이 영상의 프레임 목록 + 샘플링 설정
```
frames.json (인퍼런스 단계의 입력 명세) 는 다음과 같습니다. 
```
{
  "video_id": "G_VTkkb34gw",
  "num_frames": 16,
  "sampling": "uniform",
  "frame_size": "448x448",
  "source_duration": 268,
  "frames": [
    "finevideo/sports/G_VTkkb34gw/frames/frame_001.jpg",
    "finevideo/sports/G_VTkkb34gw/frames/frame_002.jpg"
  ],
  "sampling_config_hash": "a1b2c3"
}
```
* sampling_config_hash - 캐싱/멱등성에 활용, 샘플링 설정이 바뀌면 해시가 달라져 재샘플링


### 2. 영상 샘플링 하기 ###

ffmpeg, jq 를 그라비톤 인스턴스에 설치합니다.
```
sudo apt update && sudo apt install -y ffmpeg jq
ffmpeg -version
jq --version
```

버킷 환경변수를 설정합니다.
```
export REGION=ap-northeast-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}

echo "\n-------------------------------------"
echo "BUCKET: $BUCKET"
```

sample_frames.sh 과 xargs 를 이용하여 영상 샘플링을 병렬로 처리 합니다.
```
cd ~/vlm-on-eks/src
chmod +x sample_frames.sh

aws s3 cp "s3://${BUCKET}/finevideo/sports/manifest.jsonl" - \
  | jq -r '.video_id' \
  | xargs -P 8 -I {} ./sample_frames.sh {}
```
* -P 8 → 동시에 8개 프로세스 (코어 수에 맞춰 조정)
* -I {} → video_id를 {} 자리에 넣어 호출

S3 에 저장된 샘플링 결과를 확이한다. (첫번째 VIDEO_ID 하나에 대해서만 상세하게 출력)
```
VIDEO_ID=$(aws s3 ls $BUCKET/finevideo/sports/ 2>/dev/null | head -n 1 | awk '{print $NF}')
aws s3 ls $BUCKET/finevideo/sports/$VIDEO_ID --recursive
aws s3 cp "s3://$BUCKET/finevideo/sports/${VIDEO_ID}frames/frames.json" - | jq
```
[결과]
````
2026-07-23 23:55:39      12582 finevideo/sports/09buIj5Z5lk/frames/frame_001.jpg
2026-07-23 23:55:39      15666 finevideo/sports/09buIj5Z5lk/frames/frame_002.jpg
2026-07-23 23:55:39      16747 finevideo/sports/09buIj5Z5lk/frames/frame_003.jpg
2026-07-23 23:55:39      31335 finevideo/sports/09buIj5Z5lk/frames/frame_004.jpg
2026-07-23 23:55:39      13958 finevideo/sports/09buIj5Z5lk/frames/frame_005.jpg
2026-07-23 23:55:39      24075 finevideo/sports/09buIj5Z5lk/frames/frame_006.jpg
2026-07-23 23:55:39      24990 finevideo/sports/09buIj5Z5lk/frames/frame_007.jpg
2026-07-23 23:55:39      15231 finevideo/sports/09buIj5Z5lk/frames/frame_008.jpg
2026-07-23 23:55:39      41572 finevideo/sports/09buIj5Z5lk/frames/frame_009.jpg
2026-07-23 23:55:39      35491 finevideo/sports/09buIj5Z5lk/frames/frame_010.jpg
2026-07-23 23:55:39      32052 finevideo/sports/09buIj5Z5lk/frames/frame_011.jpg
2026-07-23 23:55:39      18882 finevideo/sports/09buIj5Z5lk/frames/frame_012.jpg
2026-07-23 23:55:39      34502 finevideo/sports/09buIj5Z5lk/frames/frame_013.jpg
2026-07-23 23:55:39      18083 finevideo/sports/09buIj5Z5lk/frames/frame_014.jpg
2026-07-23 23:55:39      17107 finevideo/sports/09buIj5Z5lk/frames/frame_015.jpg
2026-07-23 23:55:39      21425 finevideo/sports/09buIj5Z5lk/frames/frame_016.jpg
2026-07-23 23:55:39       1095 finevideo/sports/09buIj5Z5lk/frames/frames.json
2026-07-22 16:18:31      11157 finevideo/sports/09buIj5Z5lk/metadata.json
2026-07-22 16:18:31    4032992 finevideo/sports/09buIj5Z5lk/video.mp4
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
````

> [!NOTE]
> EKS에서는 이 스크립트를 컨테이너로 감싸 Graviton 노드풀의 K8s Job으로 돌리고, manifest.jsonl의 각 줄(video_id)을 여러 Job에 나눠 병렬 처리하면 됩니다.
> ```
> EKS Indexed Job에서 shard 처리
> 프로덕션에선 각 Pod가 자기 몫만 처리해야 하죠. JOB_COMPLETION_INDEX로 manifest를 나눕니다.
> 
> #!/usr/bin/env bash
> set -euo pipefail
> 
> BUCKET="vlm-data-499514681453-ap-northeast-2"
> TOTAL_SHARDS="${TOTAL_SHARDS:?}"        # = Job completions 수
> SHARD_INDEX="${JOB_COMPLETION_INDEX:?}" # K8s Indexed Job이 주입
> 
> manifest를 로컬로 받아서
> aws s3 cp "s3://${BUCKET}/finevideo/sports/manifest.jsonl" /tmp/manifest.jsonl
> 
> 이 Pod가 맡을 줄만 골라 처리 (줄번호 % 전체shard == 내 index)
> jq -r '.video_id' /tmp/manifest.jsonl \
>   | awk -v n="${TOTAL_SHARDS}" -v k="${SHARD_INDEX}" 'NR % n == k' \
>   | while read -r VIDEO_ID; do
>       ./sample_frames.sh "${VIDEO_ID}"
>     done
> awk 'NR % n == k'가 핵심으로, 전체 줄 중 "줄번호를 shard 수로 나눈 나머지가 내 인덱스인 것"만 골라서, Pod마다 겹치지 않게 나눠 처리합니다.
> ```

### 3. InternVL3-78B 와의 연결 ####
인퍼런스 단계는 이제 영상 원본이 아니라 frames/ 아래 JPG들 + frames.json만 읽으면 됩니다.

* GPU 노드는 무거운 영상 디코딩을 할 필요가 없음 (Graviton이 이미 처리)
* 프레임이 이미 448x448·16장으로 고정 → 토큰 수가 예측 가능 → 배치 사이징 안정
* 프롬프트만 튜닝할 때 프레임은 캐시 재사용 (재샘플링 불필요)

