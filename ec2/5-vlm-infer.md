## InternVL3-78B 기반 학습 데이터 생성 (영상 자동 라벨링) ##

`** 멀티 모달 모델의 GPU 사이징 **` 에서 가장 먼저 이해해야 할 점은, 병목이 모델 가중치가 아니라 비주얼 토큰 때문에 폭발하는 KV 캐시라는 사실입니다. 텍스트와 달리 이미지와 영상은 토큰으로 변환될 때 그 수가 매우 많아지는데, 특히 영상은 이미지보다 토큰 수가 수십 배에 달하기 때문에 실제 VRAM의 대부분이 여기서 소모됩니다. 따라서 사이징을 할 때는 총 VRAM을 모델 가중치, KV 캐시, 그리고 비전 인코더와 활성값·오버헤드라는 세 덩어리로 나누어 계산하는 것이 좋습니다.

첫 번째 덩어리인 모델 가중치는 사용하는 정밀도에 따라 크기가 결정됩니다. 최상의 품질이 필요한 경우에는 양자화 없이 BF16/FP16 데이터 타입을 사용하는데, 특히 정제된 모델의 경우 양자화보다는 원본 사이즈를 그대로 사용하는 것이 데이터 품질을 유지하는 방법입니다. 이 경우 파라미터당 2바이트가 필요하므로 100B 모델은 약 200GB의 가중치를 차지하며, 품질은 가장 우수합니다. 반면 파라미터당 1바이트를 쓰는 FP8은 약 100GB로 절반까지 줄어들면서도 품질 손실이 거의 없어, H100·H200·L40S 계열에서 좋은 선택지가 됩니다. 여기서 VRAM을 더 아끼고 싶다면 파라미터당 약 0.5바이트를 쓰는 INT4(AWQ/GPTQ) 양자화로 약 50GB까지 낮출 수 있지만, 이때는 약간의 품질 저하를 감수해야 합니다.

두 번째 덩어리인 KV 캐시가 영상 인퍼런스에서 진짜 병목입니다. 100B급 모델(예: 레이어 약 80개, GQA 기준 kv_dim 약 1024)의 경우 토큰 하나당 약 320KB의 KV 캐시가 필요합니다. 여기서 문제가 되는 것이 영상의 토큰 수인데, 예를 들어 Qwen2.5-VL 계열은 프레임 수에 프레임당 토큰 수를 곱해 계산되기 때문에, 짧은 영상 하나만으로도 쉽게 1만에서 5만 토큰에 이릅니다. 시퀀스 하나가 32K 토큰이라면 KV 캐시만 약 10GB를 차지하고, 이를 16개 배치로 동시에 처리하면 KV 캐시로만 약 160GB가 필요해집니다. 결국 배치 크기와 영상 길이(프레임 수)가 VRAM 소모량을 직접적으로 좌우하게 됩니다.

세 번째 덩어리인 비전 인코더와 활성값은 상대적으로 작지만 무시할 수 없습니다. ViT 인코더와 이미지·영상 전처리 과정의 활성값으로 몇 GB에서 십몇 GB가 추가로 소요됩니다.

이런 계산을 바탕으로 배치 인퍼런스용 구성을 권장하자면, 영상 배치를 돌릴 때 "가중치만 겨우 올라가는" 구성은 KV 캐시가 부족해 배치가 제대로 돌지 않거나 OOM(메모리 부족)이 발생하므로 여유를 크게 잡아야 합니다. 품질을 최우선으로 하여 BF16을 쓴다면 최소 H100 80GB 4장(TP=4)이 필요하고, 배치나 영상 길이를 키운다면 H100/H200 8장(TP=8)까지 확보하는 것이 안전합니다. 품질과 효율의 균형을 맞추는 추천 구성인 FP8의 경우 최소 H100 80GB 2장으로 시작할 수 있으며, 배치·영상 처리를 감안하면 H100/H200 4장(TP=4)이 적절합니다. VRAM을 최대한 아끼려는 INT4 구성은 최소 A100 또는 H100 80GB 2장으로 가능하고, 배치를 고려하면 4장(TP=4)으로 확장하는 것이 좋습니다.


### 1. GPU 인스턴스 생성하기 ###
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
export INSTANCE_TYPE=g6e.48xlarge

echo "\n-------------------------------------"
echo "REGION: $REGION"
echo "ACCOUNT_ID: $ACCOUNT_ID"
echo "SG_ID: $SG_ID"
echo "SUBNET_ID: $SUBNET_ID"
echo "BUCKET: $BUCKET"
echo "INSTANCE_TYPE: $INSTANCE_TYPE"
```

NVIDIA 드라이버 + Docker가 들어간 Deep Learning Base GPU AMI(Ubuntu 22.04)를 조회한다.
```
AMI_ID=$(aws ssm get-parameter \
  --region $REGION \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query 'Parameter.Value' --output text)
echo $AMI_ID
```
GPU 인스턴스를 생성한다.
```
aws ec2 run-instances \
  --region $REGION \
  --image-id $AMI_ID \
  --instance-type $INSTANCE_TYPE \
  --security-group-ids $SG_ID \
  --subnet-id $SUBNET_ID \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":600,"VolumeType":"gp3","Throughput":500,"Iops":6000,"DeleteOnTermination":true}}]' \
  --iam-instance-profile Name=vlm-ec2-profile \
  --instance-initiated-shutdown-behavior terminate \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=model-infer}]' \
  --count 1
```
* CPU 쿼터: 계정의 "Running On-Demand G instances" 쿼터가 부족하면 생성이 실패할 수 있으니, Service Quotas 를 확인한다. 
* 용량 부족(InsufficientInstanceCapacity): 최신 GPU 인스턴스의 경우 AZ에 물량이 없을 수 있다. 이럴 땐 AZ를 바꾸거나 온디맨드 용량 예약(ODCR)을 활용한다.


### 2. 인스턴스 접속하기 ###
생성된 인스턴스를 조회하고, system manager를 이용하여 로그인한다.  
```
INSTANCE=$(aws ssm describe-instance-information --region $REGION \
  --filters "Key=tag:Name,Values=model-infer" \
  --query "InstanceInformationList[].InstanceId" \
  --output text)
echo "INSTANCE: $INSTANCE"

aws ssm start-session --target $INSTANCE --region $REGION

sudo su ubuntu
nvidia-smi --query-gpu=name --format=csv,noheader | awk 'END{print $0" * "NR}'
```

[결과]
```
NVIDIA L40S * 8
```

### 3. 모델 가중치 캐싱하기 ###

hf-cache 디렉토리를 만들고 s3 에 저장된 모델 가중치를 GPU 인스턴스로 다운로드 한다.
```
ls -ld /opt/dlami/nvme

sudo mkdir -p /opt/dlami/nvme/hf-cache
sudo chown ubuntu:ubuntu /opt/dlami/nvme/hf-cache

export REGION=ap-northeast-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}

echo "\n-------------------------------------"
echo "BUCKET: $BUCKET"

aws s3 sync s3://${BUCKET}/hf-cache/ /opt/dlami/nvme/hf-cache/ 
```

모델 가중치를 저장하는 hf-cache 디렉토리 구조는 아래와 같다.
```
/opt/dlami/nvme/hf-cache/
├── hub/
│   └── models--OpenGVLab--InternVL3-78B/
│       ├── blobs/          ← 실제 파일 내용 (해시 이름의 대용량 파일들)
│       ├── snapshots/
│       │   └── <commit-hash>/
│       │       ├── *.safetensors   ← 가중치 (blobs로의 심볼릭 링크)
│       │       ├── config.json
│       │       └── ...
│       └── refs/
└── modules/               ← trust_remote_code로 받은 커스텀 모델 코드
```


### 4. 인퍼런스 하기 ###

```
cd
git clone https://github.com/gnosia93/vlm-distillation.git
cd ~/vlm-distillation/src

docker run --rm -it --gpus all --shm-size=16g \
  -v $(pwd):/work -w /work \
  -v /opt/dlami/nvme/hf-cache:/root/.cache/huggingface \
  -e PYTHONUNBUFFERED=1 \
  --entrypoint python3 \
  vllm/vllm-openai:v0.6.6.post1 \
  s3_infer.py
```
> [!NOTE]
> * -v /opt/dlami/nvme/hf-cache:/root/.cache/huggingface 에서 /opt/dlami/nvme/hf-cache 는 호스트 경로이고 /root/.cache/huggingface 는 컨테이너 경로이다. 호스트 경로에 있는 모델 가중치를 컨테이너에서 실행되는 모델에서 읽어간다.    
> * -v $(pwd):/work 는 s3_infer.py 있는 로컬 디렉토가 컨테이너의 /work 디렉토리에 매핑.
> * -w /work 는 컨테이너 안에서 명령이 실행될 작업 디렉토리(working directory)를 지정하는 옵션.
   
[결과]
```

```

### 5. S3 로 업로드 된 인퍼런스 결과 확인하기 ###
S3에 저장된 파일(a1b2c3d4e5f6.json) 내용
```
  {
    "video_id": "G_VTkkb34gw",
    "model": "OpenGVLab/InternVL3-78B",
    "prompt": "스포츠 종목과 주요 동작을 한국어로 설명해줘.",
    "answer": "16장의 프레임을 보면 실내 코트에서 진행되는 농구 경기로...",
    "run_id": "a1b2c3d4e5f6",
    "created_at": "2026-07-24T05:12:33.123456+00:00",
    "sampling_params": { "temperature": 0.2, "top_p": 0.9, "max_tokens": 512 },
    "frames_ref": {
      "num_frames": 16, "frame_size": "448x448",
      "sampling": "uniform", "sampling_config_hash": "a1b2c3"
    }
  }
```

## 인스턴스 삭제 ##
```
aws ec2 terminate-instances --instance-ids $INSTANCE --region $REGION
```







