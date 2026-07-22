### 1. g7e.24xlarge 인스턴스 생성 ###

* 1) 환경설정
```
REGION=ap-northeast-2
KEY_NAME=my-key                 # 기존 EC2 키페어 이름
SG_ID=sg-xxxxxxxx               # SSH(22) 열린 보안그룹
SUBNET_ID=subnet-xxxxxxxx       # GPU 용량 있는 AZ의 서브넷
```

* 2) GPU 드라이버 포함 AMI 조회 (SSM)
NVIDIA 드라이버 + Docker가 들어간 Deep Learning Base GPU AMI(Ubuntu 22.04)를 조회한다.
```
AMI_ID=$(aws ssm get-parameter \
  --region $REGION \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query 'Parameter.Value' --output text)

echo $AMI_ID
```

* 3) 인스턴스 생성




### 2.소스 다운로드 ###


### 3. 실행하기 ###
```
docker run --rm --gpus all --shm-size=16g \
  -v $(pwd):/work -w /work \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:v0.6.6.post1 \
  python simple_infer.py
```

* -v ~/.cache/huggingface:...는 78B 가중치(약 150GB)를 한 번 받아 캐시해두는 용도예요. 처음 실행 때 HuggingFace에서 다운로드하느라 시간이 좀 걸립니다.


* 도커 없이 직접 실행하려면:
```
pip install vllm==0.6.6.post1
python simple_infer.py
```

#### 기대 동작 ####
* 4장 GPU에 InternVL3-78B가 텐서 병렬로 로드됩니다 (로딩 수 분).
* mock 이미지 3장에 대한 한국어 설명이 콘솔에 출력돼요. 예: "파란 배경 가운데 빨간 원이 있습니다" 같은 응답.
* 이게 "한 대에서 78B가 4-GPU로 정상 로드되고 추론까지 되는지" 확인하는 가장 작은 검증판입니다.
* 여기서 잘 돌면, 앞서 만든 S3 배치 버전으로 확장하는 건 입력을 mock에서 매니페스트로 바꾸기만 하면 돼요.
* InternVL3-78B는 gated 모델일 수 있어서, 처음 받을 때 huggingface-cli login으로 토큰 인증이 필요할 수 있습니다.
  
