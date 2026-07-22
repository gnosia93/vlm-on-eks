## 데이터셋 준비하기 ##
### FineVideo 데이터셋의 의해 ###

* 영상 약 43,000개 / 3,400시간 구성되어있고 전체 용량이 수백 GB~TB 정도이다.
* WebDataset 포맷: tar 샤드 안에 .mp4(영상)와 .json(메타데이터)가 쌍으로 들어 있다.
* 각 샘플의 JSON에는 자체 택소노미 기반 카테고리(예: content_parent_category, content_fine_category)와 YouTube 메타데이터가 들어 있다.
* 게이트 데이터셋으로, HF 페이지에서 라이선스(CC) 동의를 먼저 해야 하고, 다운로드 시 HF_TOKEN이 필요하다.

> [!IMPORTANT]
> CC 라이선스 영상이라 재배포/저장 시 원본 라이선스와 저작자 표시(attribution) 조건을 지켜야 하는데, JSON의 provenance 필드를 함께 S3에 저장해두면 나중에 출처 추적이 된다.

### 1. EC2 생성하기 ###

```
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export REGION=ap-northeast-2
export SG_ID=$SG_ID
export SUBNET_ID=$SUBNET_ID

echo "ACCOUNT_ID: $ACCOUNT_ID"
echo "REGION: $REGION"
echo "SG_ID: $SG_ID"
echo "SUBNET_ID: $SUBNET_ID" 
```

데이터 준비 단계에서는 네트워크 대역폭과 디스크 성능이 좋은 CPU 인스턴스가 필요하다.
* 인스턴스: m7g.4xlarge
* 스토리지: 임시 스크래치용 로컬 NVMe 있는 타입이면 좋고, 없으면 EBS gp3 500GB~1TB.
* S3 버킷으로 다운로드 받은 파일을 업로드하므로 S3 쓰기 권한(vlm-s3-access) 이 필요하다.

```
AMI_ID=$(aws ssm get-parameter \
  --region $REGION \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query 'Parameter.Value' --output text)
echo $AMI_ID

aws ec2 run-instances \
  --region $REGION \
  --image-id $AMI_ID \
  --instance-type m7g.4xlarge \
  --security-group-ids $SG_ID \
  --subnet-id $SUBNET_ID \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":600,"VolumeType":"gp3","Throughput":500,"Iops":6000,"DeleteOnTermination":true}}]' \
  --iam-instance-profile Name=vlm-ec2-profile \
  --instance-initiated-shutdown-behavior terminate \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=internvl3-infer}]' \
  --count 1
```

인스턴스로 접속한 후 ffmpeg 및 hf 패키지를 설치한다. 
```
sudo apt-get update && sudo apt-get install -y python3-pip ffmpeg
pip install "datasets>=3.0" huggingface_hub hf_transfer boto3

export HF_TOKEN=hf_xxxxxxxxxxxx
export HF_HUB_ENABLE_HF_TRANSFER=1
```

### 2. 카테고리 필드 먼저 확인 ###
스크립트 짜기 전에 JSON 구조를 확인한다.
```
from datasets import load_dataset

ds = load_dataset("HuggingFaceFV/finevideo", split="train", streaming=True)
sample = next(iter(ds))
print(sample.keys())            # 보통 dict_keys(['mp4', 'json'])
import json
print(json.dumps(sample["json"], indent=2, ensure_ascii=False)[:3000])
```
여기서 카테고리가 어디에 들어있는지 확인하고(예: sample["json"]["content_metadata"]["content_parent_category"]),
아래 스크립트의 get_category()를 맞춰준다.

### 3. 다운로드 및 S3 적재 ###
스트리밍하면서 대상 카테고리만 골라 로컬에 임시 저장후 S3 로 업로드 한다.
```
import io
import os
import json
import boto3
from datasets import load_dataset

BUCKET = os.environ["BUCKET"]
PREFIX = "finevideo"                       # S3 최상위 경로
TARGET_CATEGORIES = {"Sports", "Cooking"}  # 원하는 카테고리로 교체
MAX_PER_CATEGORY = 500                     # 카테고리당 상한 (None이면 무제한)

s3 = boto3.client("s3")

def get_category(meta: dict) -> str | None:
    # 2단계에서 확인한 실제 경로로 맞추세요
    cm = meta.get("content_metadata", {})
    return cm.get("content_parent_category") or meta.get("categories")

def s3_put_bytes(key: str, data: bytes):
    s3.upload_fileobj(io.BytesIO(data), BUCKET, key)

def main():
    ds = load_dataset("HuggingFaceFV/finevideo", split="train", streaming=True)
    counts = {c: 0 for c in TARGET_CATEGORIES}

    for i, sample in enumerate(ds):
        meta = sample["json"]
        cat = get_category(meta)
        if cat not in TARGET_CATEGORIES:
            continue
        if MAX_PER_CATEGORY and counts[cat] >= MAX_PER_CATEGORY:
            if all(counts[c] >= MAX_PER_CATEGORY for c in TARGET_CATEGORIES):
                break
            continue

        # 안정적인 식별자 (youtube id 있으면 그걸 사용)
        vid = meta.get("youtube_id") or meta.get("id") or f"idx_{i:06d}"
        safe_cat = cat.replace(" ", "_").lower()

        # mp4 bytes 추출 (datasets 버전에 따라 형태가 다를 수 있음)
        mp4 = sample["mp4"]
        video_bytes = mp4 if isinstance(mp4, (bytes, bytearray)) else open(mp4, "rb").read()

        base = f"{PREFIX}/{safe_cat}/{vid}"
        s3_put_bytes(f"{base}/video.mp4", video_bytes)
        s3_put_bytes(f"{base}/metadata.json",
                     json.dumps(meta, ensure_ascii=False).encode("utf-8"))

        counts[cat] += 1
        print(f"[{sum(counts.values())}] {cat} -> {base}")

    # 매니페스트(색인) 저장
    manifest = {"categories": counts, "prefix": PREFIX, "target": list(TARGET_CATEGORIES)}
    s3_put_bytes(f"{PREFIX}/manifest.json",
                 json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))
    print("done:", counts)

if __name__ == "__main__":
    main()
```

아래 명령어로 실행한다. 
```
export BUCKET=your-bucket-name
python3 prepare_finevideo.py
```

### 4. S3 데이터 레이아웃 ###
파이프라인 후속 단계(추론/파인튜닝)가 쉽게 참조하도록 카테고리별로 나눠 준다.
```
s3://<BUCKET>/finevideo/
├── manifest.json                 # 전체 색인 (카테고리별 개수 등)
├── sports/
│   ├── <video_id>/
│   │   ├── video.mp4
│   │   └── metadata.json
│   └── ...
└── cooking/
    └── <video_id>/
        ├── video.mp4
        └── metadata.json
```

#### 몇 가지 실무 팁 ####
* 중단/재개: 스트리밍은 중간에 끊기면 처음부터예요. manifest.json에 처리한 인덱스를 주기적으로 기록하거나, S3에 이미 있는 video_id는 head_object로 건너뛰게 하면 재실행이 안전해요.
* 속도: hf_transfer(위에서 켬)로 다운로드가 빨라지고, S3 업로드는 boto3의 멀티파트가 자동 처리해요. 대량이면 카테고리별로 프로세스를 나눠 병렬 실행하세요.
* 디스크: 위 스크립트는 메모리→S3 직송이라 로컬 디스크를 거의 안 써요. 영상이 아주 크면 임시 파일로 떨어뜨렸다 올리는 방식으로 바꾸세요.
* 비용: EC2와 S3 버킷을 같은 리전에 두면 업로드 전송료가 없어요.

원하시면 이 스크립트를 워크스페이스에 파일로 만들어 드리거나, 재개(resume) 로직·병렬 처리까지 넣은 버전으로 확장해 드릴게요. 그리고 2단계에서 실제 JSON 구조를 확인한 결과를 알려주시면 get_category()를 정확한 필드로 맞춰드릴게요. 대상 카테고리가 정해져 있으면 그것도 반영할게요.


## 모델 가중치 S3 저장하기 ##

```
export HF_TOKEN=hf_xxx
export HF_HUB_ENABLE_HF_TRANSFER=1

huggingface-cli download OpenGVLab/InternVL3-78B \
  --local-dir /mnt/data/internvl3-78b

aws s3 sync /mnt/data/internvl3-78b/ s3://${BUCKET}/models/internvl3-78b/
```
