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

aws s3 sync s3://${BUCKET}/models/internvl3-78b/ /opt/dlami/nvme/hf-cache/models/internvl3-78b/
```

### 4. 인퍼런스 하기 ###

영상 하나만 인스턴스 해 본다.
```
cd
git clone https://github.com/gnosia93/vlm-distillation.git
cd ~/vlm-distillation/src

VIDEO_ID=$(aws s3 ls $BUCKET/finevideo/sports/ 2>/dev/null | head -n1 | awk '{print $NF}' | tr -d '/')
echo $VIDEO_ID

docker run --rm -it --gpus all --shm-size=16g \
  -v $(pwd):/work -w /work \
  -v /opt/dlami/nvme/hf-cache/models:/models \
  -e PYTHONUNBUFFERED=1 \
  -e BUCKET="$BUCKET" \
  --entrypoint python3 \
  vllm/vllm-openai:v0.6.6.post1 \
  s3_infer.py $VIDEO_ID "이 영상을 한국어로 설명해줘."
```
> [!NOTE] 
> * -v $(pwd):/work 는 s3_infer.py 있는 로컬 디렉토가 컨테이너의 /work 디렉토리에 매핑.
> * -w /work 는 컨테이너 안에서 명령이 실행될 작업 디렉토리(working directory)를 지정하는 옵션.
   
[결과]
```
[로드 완료] video_id=09buIj5Z5lk, 프레임 16장, num_frames(명세)=16, hash=d587a6
INFO 07-23 20:47:05 config.py:510] This model supports multiple tasks: {'reward', 'score', 'embed', 'classify', 'generate'}. Defaulting to 'generate'.
INFO 07-23 20:47:05 config.py:1310] Defaulting to use mp for distributed inference
INFO 07-23 20:47:05 llm_engine.py:234] Initializing an LLM engine (v0.6.6.post1) with config: model='/models/internvl3-78b', speculative_config=None, tokenizer='/models/internvl3-78b', skip_tokenizer_init=False, tokenizer_mode=auto, revision=None, override_neuron_config=None, tokenizer_revision=None, trust_remote_code=True, dtype=torch.bfloat16, max_seq_len=8192, download_dir=None, load_format=LoadFormat.AUTO, tensor_parallel_size=8, pipeline_parallel_size=1, disable_custom_all_reduce=False, quantization=None, enforce_eager=False, kv_cache_dtype=auto, quantization_param_path=None, device_config=cuda, decoding_config=DecodingConfig(guided_decoding_backend='xgrammar'), observability_config=ObservabilityConfig(otlp_traces_endpoint=None, collect_model_forward_time=False, collect_model_execute_time=False), seed=0, served_model_name=/models/internvl3-78b, num_scheduler_steps=1, multi_step_stream_outputs=True, enable_prefix_caching=False, chunked_prefill_enabled=False, use_async_output_proc=True, disable_mm_preprocessor_cache=False, mm_processor_kwargs=None, pooler_config=None, compilation_config={"splitting_ops":["vllm.unified_attention","vllm.unified_attention_with_output"],"candidate_compile_sizes":[],"compile_sizes":[],"capture_sizes":[256,248,240,232,224,216,208,200,192,184,176,168,160,152,144,136,128,120,112,104,96,88,80,72,64,56,48,40,32,24,16,8,4,2,1],"max_capture_size":256}, use_cached_outputs=False,
WARNING 07-23 20:47:05 multiproc_worker_utils.py:312] Reducing Torch parallelism from 96 threads to 1 to avoid unnecessary CPU contention. Set OMP_NUM_THREADS in the external environment to tune this value as needed.
INFO 07-23 20:47:05 custom_cache_manager.py:17] Setting Triton cache manager to: vllm.triton_utils.custom_cache_manager:CustomCacheManager
INFO 07-23 20:47:06 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=535) INFO 07-23 20:47:06 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=535) INFO 07-23 20:47:06 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=532) INFO 07-23 20:47:06 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=532) INFO 07-23 20:47:06 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=533) INFO 07-23 20:47:06 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=537) INFO 07-23 20:47:06 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=533) INFO 07-23 20:47:06 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=537) INFO 07-23 20:47:06 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=534) INFO 07-23 20:47:07 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=534) INFO 07-23 20:47:07 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=536) INFO 07-23 20:47:07 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=536) INFO 07-23 20:47:07 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=538) INFO 07-23 20:47:07 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=538) INFO 07-23 20:47:07 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=538) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=532) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=538) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=532) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=533) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=535) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=533) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=534) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=535) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=537) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=536) INFO 07-23 20:47:08 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=534) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=537) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=536) INFO 07-23 20:47:08 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=538) WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=537) WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=534) WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=533) (VllmWorkerProcess pid=536) WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=532) WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=535) WARNING 07-23 20:47:09 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
INFO 07-23 20:47:09 shm_broadcast.py:255] vLLM message queue communication handle: Handle(connect_ip='127.0.0.1', local_reader_ranks=[1, 2, 3, 4, 5, 6, 7], buffer_handle=(7, 4194304, 6, 'psm_7aa27655'), local_subscribe_port=34529, remote_subscribe_port=None)
INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=535) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=532) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=533) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=534) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=538) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=536) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
(VllmWorkerProcess pid=537) INFO 07-23 20:47:09 model_runner.py:1094] Starting to load model /models/internvl3-78b...
Loading safetensors checkpoint shards:   0% Completed | 0/33 [00:00<?, ?it/s]
Loading safetensors checkpoint shards:   3% Completed | 1/33 [00:00<00:10,  3.05it/s]
Loading safetensors checkpoint shards:   6% Completed | 2/33 [00:00<00:10,  2.87it/s]
Loading safetensors checkpoint shards:   9% Completed | 3/33 [00:01<00:10,  2.77it/s]
Loading safetensors checkpoint shards:  12% Completed | 4/33 [00:01<00:10,  2.76it/s]
Loading safetensors checkpoint shards:  15% Completed | 5/33 [00:01<00:11,  2.46it/s]
Loading safetensors checkpoint shards:  18% Completed | 6/33 [00:02<00:10,  2.63it/s]
Loading safetensors checkpoint shards:  21% Completed | 7/33 [00:02<00:09,  2.69it/s]
Loading safetensors checkpoint shards:  24% Completed | 8/33 [00:02<00:08,  2.86it/s]
Loading safetensors checkpoint shards:  27% Completed | 9/33 [00:03<00:07,  3.07it/s]
Loading safetensors checkpoint shards:  30% Completed | 10/33 [00:03<00:07,  3.10it/s]
Loading safetensors checkpoint shards:  33% Completed | 11/33 [00:03<00:07,  3.02it/s]
Loading safetensors checkpoint shards:  36% Completed | 12/33 [00:04<00:07,  2.72it/s]
Loading safetensors checkpoint shards:  39% Completed | 13/33 [00:04<00:07,  2.66it/s]
Loading safetensors checkpoint shards:  42% Completed | 14/33 [00:05<00:07,  2.64it/s]
Loading safetensors checkpoint shards:  45% Completed | 15/33 [00:05<00:06,  2.64it/s]
Loading safetensors checkpoint shards:  48% Completed | 16/33 [00:05<00:05,  2.85it/s]
Loading safetensors checkpoint shards:  52% Completed | 17/33 [00:06<00:05,  2.78it/s]
Loading safetensors checkpoint shards:  55% Completed | 18/33 [00:06<00:05,  2.72it/s]
Loading safetensors checkpoint shards:  58% Completed | 19/33 [00:06<00:04,  2.92it/s]
Loading safetensors checkpoint shards:  61% Completed | 20/33 [00:07<00:04,  2.83it/s]
Loading safetensors checkpoint shards:  64% Completed | 21/33 [00:07<00:04,  2.68it/s]
Loading safetensors checkpoint shards:  67% Completed | 22/33 [00:07<00:03,  3.03it/s]
Loading safetensors checkpoint shards:  70% Completed | 23/33 [00:08<00:03,  3.16it/s]
Loading safetensors checkpoint shards:  73% Completed | 24/33 [00:08<00:02,  3.34it/s]
Loading safetensors checkpoint shards:  76% Completed | 25/33 [00:08<00:02,  3.22it/s]
Loading safetensors checkpoint shards:  79% Completed | 26/33 [00:08<00:02,  3.35it/s]
Loading safetensors checkpoint shards:  82% Completed | 27/33 [00:09<00:01,  3.25it/s]
Loading safetensors checkpoint shards:  85% Completed | 28/33 [00:09<00:01,  3.35it/s]
Loading safetensors checkpoint shards:  88% Completed | 29/33 [00:09<00:00,  4.01it/s]
Loading safetensors checkpoint shards:  91% Completed | 30/33 [00:10<00:00,  3.78it/s]
Loading safetensors checkpoint shards:  94% Completed | 31/33 [00:10<00:00,  3.37it/s]
Loading safetensors checkpoint shards:  97% Completed | 32/33 [00:10<00:00,  3.07it/s]
Loading safetensors checkpoint shards: 100% Completed | 33/33 [00:11<00:00,  2.94it/s]
Loading safetensors checkpoint shards: 100% Completed | 33/33 [00:11<00:00,  2.96it/s]

(VllmWorkerProcess pid=535) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=532) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=533) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=537) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=538) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=536) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=534) INFO 07-23 20:47:21 model_runner.py:1099] Loading model weights took 21.6682 GB
(VllmWorkerProcess pid=533) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
(VllmWorkerProcess pid=532) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
(VllmWorkerProcess pid=537) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
(VllmWorkerProcess pid=534) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
(VllmWorkerProcess pid=536) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
(VllmWorkerProcess pid=535) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.
(VllmWorkerProcess pid=538) WARNING 07-23 20:47:21 model_runner.py:1279] Computed max_num_seqs (min(256, 8192 // 53248)) to be less than 1. Setting it to the minimum value of 1.

(VllmWorkerProcess pid=532) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 129.95 seconds
(VllmWorkerProcess pid=532) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=536) (VllmWorkerProcess pid=532) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 129.95 seconds
INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
(VllmWorkerProcess pid=536) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=536) INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
(VllmWorkerProcess pid=535) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 129.98 seconds
(VllmWorkerProcess pid=535) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=535) INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
(VllmWorkerProcess pid=533) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 129.99 seconds
(VllmWorkerProcess pid=533) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=533) INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
(VllmWorkerProcess pid=537) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 129.99 seconds
(VllmWorkerProcess pid=537) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=537) INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
(VllmWorkerProcess pid=538) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 130.00 seconds
(VllmWorkerProcess pid=538) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=538) INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 130.01 seconds
INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
(VllmWorkerProcess pid=534) INFO 07-23 20:49:31 worker.py:241] Memory profiling takes 130.04 seconds
(VllmWorkerProcess pid=534) INFO 07-23 20:49:31 worker.py:241] the current vLLM instance can use total_gpu_memory (44.39GiB) x gpu_memory_utilization (0.92) = 40.84GiB
(VllmWorkerProcess pid=534) INFO 07-23 20:49:31 worker.py:241] model weights take 21.67GiB; non_torch_memory takes 0.36GiB; PyTorch activation peak memory takes 15.73GiB; the rest of the memory reserved for KV Cache is 3.09GiB.
INFO 07-23 20:49:32 distributed_gpu_executor.py:57] # GPU blocks: 5065, # CPU blocks: 6553
INFO 07-23 20:49:32 distributed_gpu_executor.py:61] Maximum concurrency for 8192 tokens per request: 9.89x
(VllmWorkerProcess pid=538) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
(VllmWorkerProcess pid=532) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
(VllmWorkerProcess pid=533) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
(VllmWorkerProcess pid=537) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
(VllmWorkerProcess pid=535) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
(VllmWorkerProcess pid=536) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
(VllmWorkerProcess pid=534) INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
INFO 07-23 20:49:35 model_runner.py:1415] Capturing cudagraphs for decoding. This may lead to unexpected consequences if the model is not static. To run the model in eager mode, set 'enforce_eager=True' or use '--enforce-eager' in the CLI. If out-of-memory error occurs during cudagraph capture, consider decreasing `gpu_memory_utilization` or switching to eager mode. You can also reduce the `max_num_seqs` as needed to decrease memory usage.
Capturing CUDA graph shapes:  97%|█████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████▍   | 34/35 [00:30<00:00,  1.75it/s](VllmWorkerProcess pid=533) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
(VllmWorkerProcess pid=537) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
(VllmWorkerProcess pid=532) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
(VllmWorkerProcess pid=538) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
(VllmWorkerProcess pid=536) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
Capturing CUDA graph shapes: 100%|█████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 35/35 [00:31<00:00,  1.12it/s]
INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
(VllmWorkerProcess pid=535) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
(VllmWorkerProcess pid=534) INFO 07-23 20:50:06 model_runner.py:1535] Graph capturing finished in 31 secs, took 0.36 GiB
INFO 07-23 20:50:06 llm_engine.py:431] init engine (profile, create kv cache, warmup model) took 165.02 seconds
INFO 07-23 20:50:06 preprocess.py:215] Your model uses the legacy input pipeline instead of the new multi-modal processor. Please note that the legacy pipeline will be removed in a future release. For more details, see: https://github.com/vllm-project/vllm/issues/10114
Processed prompts: 100%|███████████████████████████████████████████████████████████████████████████████████| 1/1 [00:15<00:00, 15.47s/it, est. speed input: 280.66 toks/s, output: 8.73 toks/s]

============================================================
[video_id] 09buIj5Z5lk
[프롬프트] 이 영상을 한국어로 설명해줘.
[응답]
이 영상은 크리켓 경기의 한 장면을 보여줍니다. 파키스탄이 104/1로 경기를 진행 중이며, 목표점은 288점입니다. 투수는 라즈 아흐메드가 등판하고 있습니다. 타자는 공을 치고 뛰기 시작하지만, 수비수들이 빠르게 반응하여 아웃을 성공합니다. 이후 다른 타자가 등장하여 공을 치지만, 또다시 아웃됩니다. 팀원들이 기뻐하며 축하합니다.
------------------------------------------------------------
[저장 완료] s3://vlm-data-499514681453-ap-northeast-2/finevideo/sports/09buIj5Z5lk/inference/042dd539417d.json
============================================================
INFO 07-23 20:50:22 multiproc_worker_utils.py:140] Terminating local vLLM worker processes
(VllmWorkerProcess pid=532) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=533) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=535) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=534) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=536) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=537) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=538) INFO 07-23 20:50:22 multiproc_worker_utils.py:247] Worker exiting
[rank0]:[W723 20:50:29.194266029 ProcessGroupNCCL.cpp:1250] Warning: WARNING: process group has NOT been destroyed before we destruct ProcessGroupNCCL. On normal program exit, the application should call destroy_process_group to ensure that any pending NCCL operations have finished in this process. In rare cases this process can exit before this point and block the progress of another member of the process group. This constraint has always been present,  but this warning has only been added since PyTorch 2.4 (function operator())
```

### 5. S3 로 업로드 된 인퍼런스 결과 확인하기 ###

```
aws s3 cp "s3://vlm-data-499514681453-ap-northeast-2/finevideo/sports/09buIj5Z5lk/inference/042dd539417d.json" - | jq
```

[결과]
```
{
  "video_id": "09buIj5Z5lk",
  "model": "/models/internvl3-78b",
  "prompt": "이 영상을 한국어로 설명해줘.",
  "answer": "이 영상은 크리켓 경기의 한 장면을 보여줍니다. 파키스탄이 104/1로 경기를 진행 중이며, 목표점은 288점입니다. 투수는 라즈 아흐메드가 등판하고 있습니다. 타자는 공을 치고 뛰기 시작하지만, 수비수들이 빠르게 반응하여 아웃을 성공합니다. 이후 다른 타자가 등장하여 공을 치지만, 또다시 아웃됩니다. 팀원들이 기뻐하며 축하합니다.",
  "run_id": "042dd539417d",
  "created_at": "2026-07-24T03:50:22.388737+00:00",
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
}
```

[!NOTE]
> 파일명 042dd539417d 생성 규칙
> ```
> run_id = hashlib.sha256(
>      f"{prompt}|{MODEL}|{sampling_config_hash}".encode()
> ).hexdigest()[:12]
>
> 프롬프트 | 모델 | 프레임해시 를 이어붙여 SHA-256 → 앞 12자리. 
> 프롬프트 바꾸면 → 새 파일로 쌓임, 같은 프롬프트 재실행 → 덮어씀 (최신 1개 유지)
> ```


## TODO ##

* s3 에 저장된 전체 목록에 대해서 병렬로 배치 인퍼런스 하는 코드 작성 -> 필요한 경우 k8s job 으로 실행.
* "3. 프롬프트 설계 및 출력 스키마 확정" 에서 만든 프롬프트 기반으로 출력 json 포맷을 수정
  
## 인스턴스 삭제 ##
```
aws ec2 terminate-instances --instance-ids $INSTANCE --region $REGION
```







