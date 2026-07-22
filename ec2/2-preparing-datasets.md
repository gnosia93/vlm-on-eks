
### FineVideo 데이터셋의 의해 ###

* 영상 약 43,000개 / 3,400시간 구성되어있고 전체 용량이 수백 GB~TB 정도이다.
* WebDataset 포맷: tar 샤드 안에 .mp4(영상)와 .json(메타데이터)가 쌍으로 들어 있다.
* 각 샘플의 JSON에는 자체 택소노미 기반 카테고리(예: content_parent_category, content_fine_category)와 YouTube 메타데이터가 들어 있다.
* 게이트 데이터셋으로, HF 페이지에서 라이선스(CC) 동의를 먼저 해야 하고, 다운로드 시 HF_TOKEN이 필요하다.

> [!IMPORTANT]
> CC 라이선스 영상이라 재배포/저장 시 원본 라이선스와 저작자 표시(attribution) 조건을 지켜야 하는데, JSON의 provenance 필드를 함께 S3에 저장해두면 나중에 출처 추적이 된다.

### EC2 생성하기 ###

데이터 준비 단계에서는 네트워크 대역폭과 디스크 성능이 좋은 CPU 인스턴스가 필요하다.
* 인스턴스: m7i.4xlarge 또는 c7i.4xlarge 정도 (네트워크 좋고 vCPU 넉넉). 대량이면 network-optimized(m7in)도 고려.
* 스토리지: 임시 스크래치용 로컬 NVMe 있는 타입이면 좋고, 없으면 EBS gp3 500GB~1TB.
* S3 버킷으로 다운로드 받은 파일을 업로드하므로 S3 쓰기 권한(vlm-s3-access) 이 필요하다.

```
aws ec2 run-instances \
  --iam-instance-profile Name=vlm-ec2-profile \
  --instance-type m7i.4xlarge \
  --image-id <ubuntu-22.04-ami> \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":1000,"VolumeType":"gp3"}}]' \
  ... (subnet, security-group 등)
```
인스턴스로 접속한 후 ffmpeg 및 hf 패키지를 설치한다. 
```
sudo apt-get update && sudo apt-get install -y python3-pip ffmpeg
pip install "datasets>=3.0" huggingface_hub hf_transfer boto3

export HF_TOKEN=hf_xxxxxxxxxxxx
export HF_HUB_ENABLE_HF_TRANSFER=1
```
