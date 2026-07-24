## 데이터셋 준비 ##

본 워크샵에서는 FineVideo 데이터셋을 허깅페이스로 부터 다운로드 받아서 S3 에 저장합니다.
FineVideo는 약 43,000개의 영상, 총 3,400시간 분량으로 구성된 대규모 영상 데이터셋으로, 전체 용량은 수백 GB에서 TB에 이릅니다. 데이터는 parquet 포맷으로 저장되어 있으며, 그 안에는 실제 영상인 mp4 파일과 메타데이터인 json이 쌍을 이루어 담겨 있습니다. 각 샘플의 JSON에는 FineVideo 자체 택소노미(taxonomy)를 기반으로 한 카테고리 정보(예: content_parent_category, content_fine_category)와 함께 원본 YouTube 메타데이터가 포함되어 있습니다. 다만 이 데이터셋은 게이트(gated) 데이터셋이기 때문에, 먼저 Hugging Face 페이지에서 CC 라이선스에 동의해야 하며, 다운로드 시에는 HF_TOKEN이 필요합니다.

### 1. hf 토큰 발급 ###
* https://huggingface.co/ 이동하여 회원 가입 후, 
* https://huggingface.co/settings/tokens 로 이동하여 우측 상단의 + Create new token 버튼을 클릭한 후, 
* Read 타입의 토큰을 발급 받는다. 
![](https://github.com/gnosia93/vlm-on-eks/blob/main/images/hf-token.png)

### 2. ffmpeg / hf 설치 ###

```
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

export REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export SG_ID=$(aws ec2 describe-security-groups --region $REGION \
  --filters "Name=group-name,Values=vlm-sg" \
  --query "SecurityGroups[].GroupId" \
  --output text)
export SUBNET_ID=$(aws ec2 describe-subnets --region $REGION \
  --filters "Name=tag:Name,Values=vlm-pub-subnet-2" \
  --query "Subnets[0].SubnetId" \
  --output text)
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}

echo -e "\n-------------------------------------"
echo "REGION: $REGION"
echo "ACCOUNT_ID: $ACCOUNT_ID"
echo "SG_ID: $SG_ID"
echo "SUBNET_ID(2nd): $SUBNET_ID"
echo "BUCKET: $BUCKET"
```

ffmpeg 및 hf 패키지를 설치한다. 
```
sudo dnf install -y python3-pip tar xz

curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o ffmpeg.tar.xz
tar xf ffmpeg.tar.xz
sudo cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/
ffmpeg -version

pip install "python-dateutil==2.9.0"
pip install "datasets>=3.0" huggingface_hub hf_transfer boto3
```

### 3. 카테고리 필드 먼저 확인 ###
위에서 발급받은 hf 토큰을 아래와 같이 설정하고, 
```
export HF_TOKEN=hf_xxxxxxxxxxxx
export HF_XET_HIGH_PERFORMANCE=1
```

https://huggingface.co/datasets/HuggingFaceFV/finevideo 이동하여 Gate Model 에 대한 License 에 동의 한 후, 
아래 파이썬 스크립트를 이용하여 JSON 구조를 확인한다. 
```
pip install -U --user polars

git clone https://github.com/gnosia93/vlm-distillation.git
cd vlm-distillation/src

python3 inspect_pl.py
```
[결과]
![](https://github.com/gnosia93/vlm-on-eks/blob/main/images/inspect_pl.png)


### 4. 다운로드 및 S3 적재 ###
S3 에 접근 가능하도록 설정한다.  
```
cat > s3-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "VlmDataBucketRW",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    },
    {
      "Sid": "VlmDataBucketList",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::${BUCKET}"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name VlmEKS_Role \
  --policy-name VlmDataS3Access \
  --policy-document file://s3-policy.json
```

스트리밍하면서 대상 카테고리만 골라 로컬에 임시 저장후 S3 로 업로드 한다.
```
tmux new -s ingest
python3 prepare_finevideo.py
```
[결과]
```
/home/ubuntu/.local/lib/python3.10/site-packages/huggingface_hub/constants.py:298: FutureWarning: The `HF_HUB_ENABLE_HF_TRANSFER` environment variable is deprecated as 'hf_transfer' is not used anymore. Please use `HF_XET_HIGH_PERFORMANCE` instead to enable high performance transfer with Xet. Visit https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables#hfxethighperformance for more details.
  warnings.warn(
data/train-00000-of-01357.parquet: downloading bytes: ███████████████████████████████████████████████████████████████████████████████████████████████████████|  493MB, 30.5MB/s
data/train-00000-of-01357.parquet: reconstructing file: 100%|███████████████████████████████████████████████████████████████████████████████████████|  494MB /  494MB, 33.8MB/s
[shard 00000/10] sports 2개 업로드 (누적 2)
data/train-00001-of-01357.parquet: downloading bytes: ███████████████████████████████████████████████████████████████████████████████████████████████████████|  565MB, 24.3MB/s
data/train-00001-of-01357.parquet: reconstructing file: 100%|███████████████████████████████████████████████████████████████████████████████████████|  566MB /  566MB, 32.9MB/s
[shard 00001/10] sports 4개 업로드 (누적 6)
data/train-00002-of-01357.parquet: downloading bytes: ███████████████████████████████████████████████████████████████████████████████████████████████████████|  593MB, 27.1MB/s
data/train-00002-of-01357.parquet: reconstructing file: 100%|███████████████████████████████████████████████████████████████████████████████████████|  593MB /  593MB, 36.0MB/s
[shard 00002/10] sports 3개 업로드 (누적 9)
```

> [!TIP]
> tmux(terminal multiplexer)는 터미널 세션을 백그라운드에서 계속 살아있게 유지해주는 도구입니다. 가장 큰 장점은 접속을 끊어도 그 안에서 돌던 프로세스가 죽지 않고 계속 실행된다는 점인데, 예를 들어 SSH로 서버에 붙어 작업하다가 연결이 끊기더라도 tmux 세션 안에서 돌던 작업은 중단되지 않고 그대로 이어집니다. 또한 나중에 tmux attach 명령으로 다시 접속하면 하던 화면 그대로 복귀할 수 있으며, 창을 분할하거나 여러 세션을 동시에 관리하는 것도 가능합니다.
> ```
> tmux new -s ingest         # ingest 세션 새로 만들기
>                            # (작업 실행 후)
> Ctrl+b 누르고 d              # detach = 세션 빠져나오기 (작업은 계속 돌아감)
> 
> tmux ls                     # 살아있는 세션 목록 보기
> tmux attach -t ingest       # ingest 세션에 다시 접속
> tmux kill-session -t ingest # ingest 세션 완전히 종료
> ```

### 5. S3 데이터 레이아웃 ###

파이프라인 후속 단계(추론/파인튜닝)가 쉽게 참조하도록 카테고리별로 나눠서 저장된다. 여러 카테고리가 있으나 본 워크샵에서는 sports 카테고리 데이터만 받아서 S3 에 저장하였다.
```
s3://<BUCKET>/finevideo/
├── manifest.json                 # 전체 색인 (카테고리별 개수 등)
├── sports/
    ├── <video_id>/
    │   ├── video.mp4
    │   └── metadata.json
    └── ...
```

버킷안의 오브젝트 리스트를 조회한다. 
```
aws s3 ls $BUCKET/finevideo/sports/
```
[결과]
```
                           PRE 09buIj5Z5lk/
                           PRE 0nWa-vCCZkc/
                           PRE 0pV4gxuJCCQ/
                           ...
                           PRE _done/
2026-07-23 01:19:20         98 manifest.json
2026-07-23 01:19:20       9174 manifest.jsonl
```
전반적인 정보를 저장하고 있는 menifest를 조회한다. 
```
aws s3 cp s3://$BUCKET/finevideo/sports/manifest.json - | jq .
```
[결과]
```
{
  "category": "Sports",
  "prefix": "finevideo/sports",
  "total_videos": 25,
  "n_shards": 10
}
```
영상에 대한 상세 정보를 가지고 있는 manifest.jsonl 을 조회한다.
```
aws s3 cp s3://$BUCKET/finevideo/sports/manifest.jsonl - | jq .
```
[결과]
```
{
  "video_id": "G_VTkkb34gw",
  "category": "Sports",
  "fine_category": "Career Highlights",
  "video_key": "finevideo/sports/G_VTkkb34gw/video.mp4",
  "metadata_key": "finevideo/sports/G_VTkkb34gw/metadata.json",
  "channel": "QueenslandPolice",
  "title": "Life at the Academy - Queensland Police Service",
  "upload_date": "20230921",
  "duration": 268,
  "resolution": "640x360"
}
{
  "video_id": "1TtcXC_u4r4",
  "category": "Sports",
  "fine_category": "Career Highlights",
  "video_key": "finevideo/sports/1TtcXC_u4r4/video.mp4",
  "metadata_key": "finevideo/sports/1TtcXC_u4r4/metadata.json",
  "channel": "Interserve Learning & Employment",
  "title": "Kerry Mills | Trainer Assessor",
  "upload_date": "20170223",
  "duration": 251,
  "resolution": "640x360"
}
{
  "video_id": "LttCU4RK-Zc",
  "category": "Sports",
```


## 모델 가중치 S3 저장하기 ##

```
sudo apt update && sudo apt install -y unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
aws --version
```

hf 로 OpenGVLab/InternVL3-78B 모델의 가중치를 다운로드 받고, S3 로 업로드 한다. 
> [!WARNING]
> --local-dir flat 다운로드 방식의 특징과 주의점
>
> hf download ... --local-dir <경로>로 받으면 파일이 HF 캐시 구조(blobs/snapshots/)가 아니라 한 폴더에 평평하게(flat) 저장된다
> - 캐시로 인식 안 됨: hf-cache 위치에 넣어도 캐시 히트가 안 되고 Hub에서 재다운로드한다.
> - 용량 이점: 심볼릭 링크가 없어 중복이 없고, S3 왕복 시 링크 깨짐 걱정도 없어 S3 경유에 적합하다.
>
> 로딩 시: MODEL을 Hub ID("OpenGVLab/InternVL3-78B")가 아니라 로컬 경로("/models/internvl3-78b")로 직접 지정해야 한다. HF_HUB_OFFLINE=1도 함께 설정 권장
```
export PATH=$PATH:/home/ubuntu/.local/bin
sudo mkdir -p /mnt/data
sudo chown ubuntu:ubuntu /mnt/data

# flat 구조로 다운로드
hf download OpenGVLab/InternVL3-78B --local-dir /mnt/data/internvl3-78b
hf download OpenGVLab/InternVL3-1B --local-dir /mnt/data/internvl3-1b

# S3에 업로드 (flat 그대로)
echo "model weight loading in $BUCKET"
aws s3 sync /mnt/data/internvl3-78b/ s3://${BUCKET}/models/internvl3-78b/
aws s3 sync /mnt/data/internvl3-1b/ s3://${BUCKET}/models/internvl3-1b/

# S3 확인
aws s3 ls s3://${BUCKET}/models/internvl3-78b/ 2>/dev/null | head -n 15
aws s3 ls s3://${BUCKET}/models/internvl3-1b/ 2>/dev/null | head -n 15
```

[결과]
```
                           PRE .cache/
                           PRE examples/
2026-07-24 03:24:30       1634 .gitattributes
2026-07-24 03:24:30      35864 README.md
2026-07-24 03:24:30        790 added_tokens.json
2026-07-24 03:24:30       6346 config.json
2026-07-24 03:24:30       5548 configuration_intern_vit.py
2026-07-24 03:24:30       4036 configuration_internvl_chat.py
2026-07-24 03:24:30      15309 conversation.py
2026-07-24 03:24:30         69 generation_config.json
2026-07-24 03:24:30    1671853 merges.txt
2026-07-24 03:24:30 4988569440 model-00001-of-00033.safetensors
2026-07-24 03:24:32 4937253584 model-00002-of-00033.safetensors
2026-07-24 03:24:37 4903161648 model-00003-of-00033.safetensors
2026-07-24 03:24:37 4781670848 model-00004-of-00033.safetensors
```
