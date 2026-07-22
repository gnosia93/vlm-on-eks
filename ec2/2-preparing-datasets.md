## 데이터셋 준비 ##

본 워크샵에서는 FineVideo 데이터셋을 허깅페이스로 부터 다운로드 받아서 S3 에 저장합니다.
FineVideo는 약 43,000개의 영상, 총 3,400시간 분량으로 구성된 대규모 영상 데이터셋으로, 전체 용량은 수백 GB에서 TB에 이릅니다. 데이터는 parquet 포맷으로 저장되어 있으며, 그 안에는 실제 영상인 mp4 파일과 메타데이터인 json이 쌍을 이루어 담겨 있습니다. 각 샘플의 JSON에는 FineVideo 자체 택소노미(taxonomy)를 기반으로 한 카테고리 정보(예: content_parent_category, content_fine_category)와 함께 원본 YouTube 메타데이터가 포함되어 있습니다. 다만 이 데이터셋은 게이트(gated) 데이터셋이기 때문에, 먼저 Hugging Face 페이지에서 CC 라이선스에 동의해야 하며, 다운로드 시에는 HF_TOKEN이 필요합니다.

### 1. hf 토큰 발급 ###
* https://huggingface.co/ 이동하여 회원 가입 후, 
* https://huggingface.co/settings/tokens 로 이동하여 우측 상단의 + Create new token 버튼을 클릭한 후, 
* Read 타입의 토큰을 발급 받는다. 
![](https://github.com/gnosia93/vlm-on-eks/blob/main/images/hf-token.png)

### 2. EC2 생성하기 ###

```
export REGION=ap-northeast-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export SG_ID=$(aws ec2 describe-security-groups --region $REGION \
  --filters "Name=group-name,Values=vlm-sg" \
  --query "SecurityGroups[].GroupId" \
  --output text)
export SUBNET_ID=$(aws ec2 describe-subnets --region $REGION \
  --filters "Name=tag:Name,Values=vlm-public-subnet" \
  --query "Subnets[0].SubnetId" \
  --output text)
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}

echo "\n-------------------------------------"
echo "REGION: $REGION"
echo "ACCOUNT_ID: $ACCOUNT_ID"
echo "SG_ID: $SG_ID"
echo "SUBNET_ID: $SUBNET_ID"
echo "BUCKET: $BUCKET"
```

데이터 준비 단계에서는 네트워크 대역폭과 디스크 성능이 좋은 CPU 인스턴스가 필요하다.
* 인스턴스: m7g.4xlarge
* 스토리지: 임시 스크래치용 로컬 NVMe 있는 타입이면 좋고, 없으면 EBS gp3 500GB~1TB.
* S3 버킷으로 다운로드 받은 파일을 업로드하므로 S3 쓰기 권한(vlm-s3-access) 이 필요하다.

```
AMI_ID=$(aws ssm get-parameter \
  --region $REGION \
  --name /aws/service/canonical/ubuntu/server/22.04/stable/current/arm64/hvm/ebs-gp2/ami-id \
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
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=data-preprocessing}]' \
  --count 1
```

### 3. 인스턴스 접속하기 ####
system manager 를 이용하여 인스턴스에 접속 한다. 클라이이언트가 맥 os 인 경우 플러그인을 설치가 필요하다. 
```
brew install --cask session-manager-plugin
```

접속할 인스턴스를 조회하고, system manager 를 이용하여 로그인한다.  
```
INSTANCE=$(aws ssm describe-instance-information --region $REGION \
  --filters "Key=tag:Name,Values=data-preprocessing" \
  --query "InstanceInformationList[].InstanceId" \
  --output text)
echo "INSTANCE: $INSTANCE"

aws ssm start-session --target $INSTANCE --region $REGION

sudo su ubuntu
```


인스턴스로 접속한 후 ffmpeg 및 hf 패키지를 설치한다. 
```
cd
sudo apt-get update && sudo apt-get install -y python3-pip ffmpeg
pip install "datasets>=3.0" huggingface_hub hf_transfer boto3
```

### 4. 카테고리 필드 먼저 확인 ###
위에서 발급받은 hf 토큰을 아래와 같이 설정하고, 
```
export HF_TOKEN=hf_xxxxxxxxxxxx
export HF_XET_HIGH_PERFORMANCE=1
```

https://huggingface.co/datasets/HuggingFaceFV/finevideo 이동하여 Gate Model 에 대한 License 에 동의 한 후, 
아래 파이썬 스크립트를 이용하여 JSON 구조를 확인한다. 
```
pip install -U --user polars

git clone https://github.com/gnosia93/vlm-on-eks.git
cd vlm-on-eks/src

python3 inspect_pl.py
```
[결과]
![](https://github.com/gnosia93/vlm-on-eks/blob/main/images/inspect_pl.png)


### 5. 다운로드 및 S3 적재 ###
스트리밍하면서 대상 카테고리만 골라 로컬에 임시 저장후 S3 로 업로드 한다.
```
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
MAC=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/mac)
ACCOUNT_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/network/interfaces/macs/${MAC}/owner-id)

export ACCOUNT_ID REGION
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}
echo "BUCKET: $BUCKET"

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

### 6. S3 데이터 레이아웃 ###

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
menifest를 조회한다. 
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

## 모델 가중치 S3 저장하기 ##

허깅페이스 cli 로 OpenGVLab/InternVL3-78B 모델의 가중치를 다운로드 받고, S3 로 업로드 한다. 
```
export PATH=$PATH:/home/ubuntu/.local/bin
sudo mkdir -p /mnt/data
sudo chown ubuntu:ubuntu /mnt/data

hf download OpenGVLab/InternVL3-78B --local-dir /mnt/data/internvl3-78b

sudo apt update && sudo apt install -y unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
aws --version

echo "model weight loading in $BUCKET"
aws s3 sync /mnt/data/internvl3-78b/ s3://${BUCKET}/models/internvl3-78b/

aws s3 ls s3://${BUCKET}/models/internvl3-78b/
```
[결과]
```
                          PRE .cache/
                           PRE examples/
2026-07-22 16:51:34       1634 .gitattributes
2026-07-22 16:51:34      35864 README.md
2026-07-22 16:51:34        790 added_tokens.json
2026-07-22 16:51:34       6346 config.json
2026-07-22 16:51:34       5548 configuration_intern_vit.py
2026-07-22 16:51:34       4036 configuration_internvl_chat.py
2026-07-22 16:51:34      15309 conversation.py
2026-07-22 16:51:34         69 generation_config.json
2026-07-22 16:51:34    1671853 merges.txt
2026-07-22 16:51:34 4988569440 model-00001-of-00033.safetensors
2026-07-22 16:51:35 4937253584 model-00002-of-00033.safetensors
2026-07-22 16:51:38 4903161648 model-00003-of-00033.safetensors
2026-07-22 16:51:46 4781670848 model-00004-of-00033.safetensors
2026-07-22 16:51:46 4781670848 model-00005-of-00033.safetensors
...
```
