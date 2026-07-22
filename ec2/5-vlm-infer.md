## InternVL3-78B 기반 학습 데이터 생성 (영상 자동 라벨링) ##

### GPU 사이징 ###

멀티모달 모델의 GPU 사이징에서 가장 먼저 이해해야 할 점은, 병목이 모델 가중치가 아니라 비주얼 토큰 때문에 폭발하는 KV 캐시라는 사실입니다. 텍스트와 달리 이미지와 영상은 토큰으로 변환될 때 그 수가 매우 많아지는데, 특히 영상은 이미지보다 토큰 수가 수십 배에 달하기 때문에 실제 VRAM의 대부분이 여기서 소모됩니다. 따라서 사이징을 할 때는 총 VRAM을 모델 가중치, KV 캐시, 그리고 비전 인코더와 활성값·오버헤드라는 세 덩어리로 나누어 계산하는 것이 좋습니다.

첫 번째 덩어리인 모델 가중치는 사용하는 정밀도에 따라 크기가 결정됩니다. 최상의 품질이 필요한 경우에는 양자화 없이 BF16/FP16 데이터 타입을 사용하는데, 특히 정제된 모델의 경우 양자화보다는 원본 사이즈를 그대로 사용하는 것이 데이터 품질을 유지하는 방법입니다. 이 경우 파라미터당 2바이트가 필요하므로 100B 모델은 약 200GB의 가중치를 차지하며, 품질은 가장 우수합니다. 반면 파라미터당 1바이트를 쓰는 FP8은 약 100GB로 절반까지 줄어들면서도 품질 손실이 거의 없어, H100·H200·L40S 계열에서 좋은 선택지가 됩니다. 여기서 VRAM을 더 아끼고 싶다면 파라미터당 약 0.5바이트를 쓰는 INT4(AWQ/GPTQ) 양자화로 약 50GB까지 낮출 수 있지만, 이때는 약간의 품질 저하를 감수해야 합니다.

두 번째 덩어리인 KV 캐시가 영상 인퍼런스에서 진짜 병목입니다. 100B급 모델(예: 레이어 약 80개, GQA 기준 kv_dim 약 1024)의 경우 토큰 하나당 약 320KB의 KV 캐시가 필요합니다. 여기서 문제가 되는 것이 영상의 토큰 수인데, 예를 들어 Qwen2.5-VL 계열은 프레임 수에 프레임당 토큰 수를 곱해 계산되기 때문에, 짧은 영상 하나만으로도 쉽게 1만에서 5만 토큰에 이릅니다. 시퀀스 하나가 32K 토큰이라면 KV 캐시만 약 10GB를 차지하고, 이를 16개 배치로 동시에 처리하면 KV 캐시로만 약 160GB가 필요해집니다. 결국 배치 크기와 영상 길이(프레임 수)가 VRAM 소모량을 직접적으로 좌우하게 됩니다.

세 번째 덩어리인 비전 인코더와 활성값은 상대적으로 작지만 무시할 수 없습니다. ViT 인코더와 이미지·영상 전처리 과정의 활성값으로 몇 GB에서 십몇 GB가 추가로 소요됩니다.

이런 계산을 바탕으로 배치 인퍼런스용 구성을 권장하자면, 영상 배치를 돌릴 때 "가중치만 겨우 올라가는" 구성은 KV 캐시가 부족해 배치가 제대로 돌지 않거나 OOM(메모리 부족)이 발생하므로 여유를 크게 잡아야 합니다. 품질을 최우선으로 하여 BF16을 쓴다면 최소 H100 80GB 4장(TP=4)이 필요하고, 배치나 영상 길이를 키운다면 H100/H200 8장(TP=8)까지 확보하는 것이 안전합니다. 품질과 효율의 균형을 맞추는 추천 구성인 FP8의 경우 최소 H100 80GB 2장으로 시작할 수 있으며, 배치·영상 처리를 감안하면 H100/H200 4장(TP=4)이 적절합니다. VRAM을 최대한 아끼려는 INT4 구성은 최소 A100 또는 H100 80GB 2장으로 가능하고, 배치를 고려하면 4장(TP=4)으로 확장하는 것이 좋습니다.


### 1. GPU 인스턴스 생성하기 ###
인스턴스에 필요한 정보를 설정한다. 여기서는 48GB * 8 장을 지원하는 g6e.48xlarge 를 선택한다.(VRAM 48GB * 8 = 384 GB)
```
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export REGION=ap-northeast-2
export SG_ID=$SG_ID
export SUBNET_ID=$SUBNET_ID
export INSTANCE_TYPE=g6e.48xlarge
```

NVIDIA 드라이버 + Docker가 들어간 Deep Learning Base GPU AMI(Ubuntu 22.04)를 조회한다.
```
AMI_ID=$(aws ssm get-parameter \
  --region $REGION \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query 'Parameter.Value' --output text)
echo $AMI_ID
```
인스턴스를 생성한다.
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
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=internvl3-infer}]' \
  --count 1
```
* CPU 쿼터: 계정의 "Running On-Demand G instances" 쿼터가 부족하면 생성이 실패할 수 있으니, Service Quotas 를 확인한다. 
* 용량 부족(InsufficientInstanceCapacity): 최신 GPU 인스턴스의 경우 AZ에 물량이 없을 수 있다. 이럴 땐 AZ를 바꾸거나 온디맨드 용량 예약(ODCR)을 활용한다.

> [!TIP]
> 생성된 인스턴스의 퍼블릭 IP 를 확인한다. 
> ```
> aws ec2 describe-instances --region $REGION \
>   --filters "Name=tag:Name,Values=internvl3-infer" "Name=instance-state-name,Values=running" \
>   --query 'Reservations[].Instances[].PublicIpAddress' --output text
> ```

### 2. 인스턴스 접속하기 ####
system manager 를 이용하여 인스턴스에 접속 한다. 클라이이언트가 맥 os 인 경우 플러그인을 설치가 필요하다. 
```
brew install --cask session-manager-plugin
```

접속할 인스턴스를 조회하고, system manager 를 이용하여 로그인한다.  
```
INSTANCE=$(aws ssm describe-instance-information \
  --query "InstanceInformationList[].InstanceId" --region $REGION --output text)
echo "INSTANCE: $INSTANCE"

aws ssm start-session --target $INSTANCE --region $REGION

sudo su ubuntu
nvidia-smi --query-gpu=name --format=csv,noheader
```
[결과]
```
NVIDIA L40S
NVIDIA L40S
NVIDIA L40S
NVIDIA L40S
NVIDIA L40S
NVIDIA L40S
NVIDIA L40S
NVIDIA L40S
```

### 3.소스 다운로드 ###
인퍼런스용으로 사용할 어플리케이션을 다운로드 받는다. 
```
git clone https://github.com/gnosia93/vlm-on-eks.git
cd vlm-on-eks/src
```

### 4. docker 이미지 실행하기 ###
nvlme 인스턴스 스토어 정보를 확인한다.
```
ls -ld /opt/dlami/nvme

sudo mkdir -p /opt/dlami/nvme/hf-cache
sudo chown ubuntu:ubuntu /opt/dlami/nvme/hf-cache
```

docker 이미지로 인퍼런스를 실행한다. 이때 huggingface 의 모델은 호스트 경로 /opt/dlami/nvme/hf-cache(NVME 인스턴스 스토어) 에 저장된다.
* -v /opt/dlami/nvme/hf-cache:/root/.cache/huggingface 는 78B 가중치(약 150GB)를 다운로드 받아 캐시해두는 위치로, /opt/dlami/nvme/hf-cache 는 호스트 경로이고 /root/.cache/huggingface 는 컨테이너 내부 경로이다. 즉 컨테이너 내부에서 hf 로 연결하여 가웅치를 다운로드 받으면 호스트 경로 /opt/dlami/nvme/hf-cache 에 저장된다. 
* -w /work 작업 디렉토리
```
docker run --rm -it --gpus all --shm-size=16g \
  -v $(pwd):/work -w /work \
  -v /opt/dlami/nvme/hf-cache:/root/.cache/huggingface \
  -e PYTHONUNBUFFERED=1 \
  -e HF_HUB_ENABLE_HF_TRANSFER=1 \
  --entrypoint python3 \
  vllm/vllm-openai:v0.6.6.post1 \
  simple_infer.py
```
[결과]
```
v0.6.6.post1: Pulling from vllm/vllm-openai
aece8493d397: Pull complete
c5f00d6d0d62: Download complete
12cd4d19752f: Pull complete
3d97a47c3c73: Pull complete
4f4fb700ef54: Download complete
850a3ed97a0d: Downloading [==================================================>]  3.545GB/3.545GB
71729f03dad2: Download complete
da5a484f9d74: Pull complete
45f7ea5367fe: Pull complete
f64334cb5400: Download complete
be8001762246: Downloading [===================================>               ]  305.1MB/427.2MB
117e97d3740c: Pull complete
49ce6df6e942: Download complete
87b4097c53c8: Pull complete

INFO 07-22 03:06:58 config.py:510] This model supports multiple tasks: {'classify', 'score', 'reward', 'embed', 'generate'}. Defaulting to 'generate'.
INFO 07-22 03:06:58 config.py:1310] Defaulting to use mp for distributed inference
INFO 07-22 03:06:58 llm_engine.py:234] Initializing an LLM engine (v0.6.6.post1) with config: model='OpenGVLab/InternVL3-78B', speculative_config=None, tokenizer='OpenGVLab/InternVL3-78B', skip_tokenizer_init=False, tokenizer_mode=auto, revision=None, override_neuron_config=None, tokenizer_revision=None, trust_remote_code=True, dtype=torch.bfloat16, max_seq_len=8192, download_dir=None, load_format=LoadFormat.AUTO, tensor_parallel_size=4, pipeline_parallel_size=1, disable_custom_all_reduce=False, quantization=None, enforce_eager=False, kv_cache_dtype=auto, quantization_param_path=None, device_config=cuda, decoding_config=DecodingConfig(guided_decoding_backend='xgrammar'), observability_config=ObservabilityConfig(otlp_traces_endpoint=None, collect_model_forward_time=False, collect_model_execute_time=False), seed=0, served_model_name=OpenGVLab/InternVL3-78B, num_scheduler_steps=1, multi_step_stream_outputs=True, enable_prefix_caching=False, chunked_prefill_enabled=False, use_async_output_proc=True, disable_mm_preprocessor_cache=False, mm_processor_kwargs=None, pooler_config=None, compilation_config={"splitting_ops":["vllm.unified_attention","vllm.unified_attention_with_output"],"candidate_compile_sizes":[],"compile_sizes":[],"capture_sizes":[256,248,240,232,224,216,208,200,192,184,176,168,160,152,144,136,128,120,112,104,96,88,80,72,64,56,48,40,32,24,16,8,4,2,1],"max_capture_size":256}, use_cached_outputs=False,
WARNING 07-22 03:06:59 multiproc_worker_utils.py:312] Reducing Torch parallelism from 96 threads to 1 to avoid unnecessary CPU contention. Set OMP_NUM_THREADS in the external environment to tune this value as needed.
INFO 07-22 03:06:59 custom_cache_manager.py:17] Setting Triton cache manager to: vllm.triton_utils.custom_cache_manager:CustomCacheManager
INFO 07-22 03:07:00 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=485) INFO 07-22 03:07:00 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=485) INFO 07-22 03:07:00 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=486) INFO 07-22 03:07:00 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=486) INFO 07-22 03:07:00 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
(VllmWorkerProcess pid=487) INFO 07-22 03:07:00 selector.py:120] Using Flash Attention backend.
(VllmWorkerProcess pid=487) INFO 07-22 03:07:00 multiproc_worker_utils.py:222] Worker ready; awaiting tasks
INFO 07-22 03:07:02 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=486) INFO 07-22 03:07:02 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=485) INFO 07-22 03:07:02 utils.py:918] Found nccl from library libnccl.so.2
INFO 07-22 03:07:02 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=486) INFO 07-22 03:07:02 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=485) INFO 07-22 03:07:02 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=487) INFO 07-22 03:07:02 utils.py:918] Found nccl from library libnccl.so.2
(VllmWorkerProcess pid=487) INFO 07-22 03:07:02 pynccl.py:69] vLLM is using nccl==2.21.5
(VllmWorkerProcess pid=487) WARNING 07-22 03:07:03 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=486) WARNING 07-22 03:07:03 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
WARNING 07-22 03:07:03 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
(VllmWorkerProcess pid=485) WARNING 07-22 03:07:03 custom_all_reduce.py:134] Custom allreduce is disabled because it's not supported on more than two PCIe-only GPUs. To silence this warning, specify disable_custom_all_reduce=True explicitly.
INFO 07-22 03:07:03 shm_broadcast.py:255] vLLM message queue communication handle: Handle(connect_ip='127.0.0.1', local_reader_ranks=[1, 2, 3], buffer_handle=(3, 4194304, 6, 'psm_b9a4caee'), local_subscribe_port=55397, remote_subscribe_port=None)
INFO 07-22 03:07:03 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=486) INFO 07-22 03:07:03 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=485) INFO 07-22 03:07:03 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=487) INFO 07-22 03:07:03 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
INFO 07-22 03:07:03 weight_utils.py:251] Using model weights format ['*.safetensors']
(VllmWorkerProcess pid=485) INFO 07-22 03:07:03 weight_utils.py:251] Using model weights format ['*.safetensors']
(VllmWorkerProcess pid=486) INFO 07-22 03:07:03 weight_utils.py:251] Using model weights format ['*.safetensors']
(VllmWorkerProcess pid=487) INFO 07-22 03:07:03 weight_utils.py:251] Using model weights format ['*.safetensors']

INFO 07-22 03:10:41 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=487) INFO 07-22 03:10:41 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=485) INFO 07-22 03:10:41 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=486) INFO 07-22 03:10:41 model_runner.py:1094] Starting to load model OpenGVLab/InternVL3-78B...
(VllmWorkerProcess pid=487) INFO 07-22 03:10:42 weight_utils.py:251] Using model weights format ['*.safetensors']
(VllmWorkerProcess pid=486) INFO 07-22 03:10:42 weight_utils.py:251] Using model weights format ['*.safetensors']
INFO 07-22 03:10:42 weight_utils.py:251] Using model weights format ['*.safetensors']
(VllmWorkerProcess pid=485) INFO 07-22 03:10:42 weight_utils.py:251] Using model weights format ['*.safetensors']
model-00002-of-00033.safetensors:  99%|████████████████████████████████████████████████████████████████████████████████████████████████▊ | 4.87G/4.94G [00:31<00:04, 15.7MB/s
...



Processed prompts: 100%|█████████████████████████████████████████████████████████████████| 3/3 [00:03<00:00,  1.31s/it, est. speed input: 247.73 toks/s, output: 19.10 toks/s]

============================================================
[이미지 1] 프롬프트: 이 이미지에 무엇이 보이는지 한국어로 설명해줘.
응답: 이 이미지는 파란색 배경에 가운데에 빨간색 원이 있는 깃발입니다. 이는 일본의 국기인 "히와리"입니다.
------------------------------------------------------------
[이미지 2] 프롬프트: 도형의 개수와 색을 한국어로 알려줘.
응답: 이미지에는 초록색 사각형이 세 개 있습니다.
------------------------------------------------------------
[이미지 3] 프롬프트: 이미지를 한 문장으로 한국어로 요약해줘.
응답: 이미지는 노란색 배경에 검은색 삼각형이 있습니다.
------------------------------------------------------------
INFO 07-22 04:20:44 multiproc_worker_utils.py:140] Terminating local vLLM worker processes
(VllmWorkerProcess pid=484) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=485) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=489) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=487) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=490) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=488) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
(VllmWorkerProcess pid=486) INFO 07-22 04:20:44 multiproc_worker_utils.py:247] Worker exiting
[rank0]:[W722 04:20:51.746222605 ProcessGroupNCCL.cpp:1250] Warning: WARNING: process group has NOT been destroyed before we destruct ProcessGroupNCCL. On normal program exit, the application should call destroy_process_group to ensure that any pending NCCL operations have finished in this process. In rare cases this process can exit before this point and block the progress of another member of the process group. This constraint has always been present,  but this warning has only been added since PyTorch 2.4 (function operator())
```
> [!TIP]
> hf 의 경우 익명 연결시 쓰로틀링이 걸리게 되는데 이를 회피하기 아래와 같이 두가지 파라미터를 적용한다. 좀더 빠르게 실행하기 위해서는 가중치를 다운로드 받은 후 S3 에 저장해 놓는 것이 유리하다. 
> ```
> -e HF_HUB_ENABLE_HF_TRANSFER=1
> -e HF_TOKEN=hf_xxxxxxxxxxxx
> ```

## 인프런스 결과 S3 업로드 하기 ##
.. 나중에 구현.


## 인스턴스 삭제 ##
```
aws ec2 terminate-instances --instance-ids $INSTANCE --region $REGION
```



----

## 가중치 S3 업로드 하기 ##
```
$ cd /opt/dlami/nvme/hf-cache
ubuntu@ip-10-0-1-41:/opt/dlami/nvme/hf-cache$ ls -la
total 16
drwxr-xr-x 4 ubuntu ubuntu 4096 Jul 22 10:06 .
drwxrwxrwt 4 root   root   4096 Jul 22 09:59 ..
drwxr-xr-x 4 root   root   4096 Jul 22 10:06 hub
drwxr-xr-x 2 root   root   4096 Jul 22 10:06 modules
```

* 실제 가중치 파일 위치
```
ls -la /opt/dlami/nvme/hf-cache/hub/models--OpenGVLab--InternVL3-78B/snapshots/*/
```
[결과]
```
total 140
drwxr-xr-x 2 root root 4096 Jul 22 11:13 .
drwxr-xr-x 3 root root 4096 Jul 22 10:06 ..
lrwxrwxrwx 1 root root   52 Jul 22 10:06 added_tokens.json -> ../../blobs/dd972e4080e791eab591742c1168ee7fd6279146
lrwxrwxrwx 1 root root   52 Jul 22 10:06 config.json -> ../../blobs/1778c9e50e3454fa25855dea7b4edef2d007a32a
lrwxrwxrwx 1 root root   52 Jul 22 10:06 generation_config.json -> ../../blobs/467e2ba8edc477ad1fb650f892ab5be9210522ab
lrwxrwxrwx 1 root root   52 Jul 22 10:06 merges.txt -> ../../blobs/31349551d90c7606f325fe0f11bbb8bd5fa0d7c7
lrwxrwxrwx 1 root root   76 Jul 22 10:08 model-00001-of-00033.safetensors -> ../../blobs/c527e7dfe9c1d8c0b3da1a10a6940f7dcf3caa6872078c38c3bc90413f30f99a
lrwxrwxrwx 1 root root   76 Jul 22 10:13 model-00002-of-00033.safetensors -> ../../blobs/72b68545e081c6abfa1cd5ecd53bee5a0664353873f8423e345bcfeb9da3050e
lrwxrwxrwx 1 root root   76 Jul 22 10:14 model-00003-of-00033.safetensors -> ../../blobs/bba45d9bc1710fefb525b588e1496eaf102249e36e8b007ca3db8be5fee8617c
lrwxrwxrwx 1 root root   76 Jul 22 10:17 model-00004-of-00033.safetensors -> ../../blobs/7ce11fc1502f602fc7bae2489159b292e4ea9afccd0f3b59042a42de89c5ca79
lrwxrwxrwx 1 root root   76 Jul 22 10:19 model-00005-of-00033.safetensors -> ../../blobs/aaa0c8aa08eac0bcd177c0889d03c384692c84e8aca3f9ae82604db837440c47
lrwxrwxrwx 1 root root   76 Jul 22 10:22 model-00006-of-00033.safetensors -> ../../blobs/e1523bc2d85879c3b14e94aad601d12f5399d5d37269602b057ff0eab22904b2
lrwxrwxrwx 1 root root   76 Jul 22 10:28 model-00007-of-00033.safetensors -> ../../blobs/1482a9e5aef97f1e39d0897b37bf24b5aa96f8fb13fedb54928b6e0da5eb85c0
lrwxrwxrwx 1 root root   76 Jul 22 10:31 model-00008-of-00033.safetensors -> ../../blobs/110bc8463ce8fa4c51b9492a83761aadf9c7d5ff227d4c7b461a61eedf3c3682
lrwxrwxrwx 1 root root   76 Jul 22 10:33 model-00009-of-00033.safetensors -> ../../blobs/5ba2bca20fef2912f4678b3763f2d340187a4336345c2ab2489b5749b66acbd9
lrwxrwxrwx 1 root root   76 Jul 22 10:36 model-00010-of-00033.safetensors -> ../../blobs/cbb780cc93a6ca302b698c877b0b93d0e613b8b574e629dc3d194ce8884d1c8e
lrwxrwxrwx 1 root root   76 Jul 22 10:37 model-00011-of-00033.safetensors -> ../../blobs/854ab3f066ed326cedc508e4e43179c0a0ea83ad7c6fed7cddb56fb4f2107687
lrwxrwxrwx 1 root root   76 Jul 22 10:39 model-00012-of-00033.safetensors -> ../../blobs/5727c85fe014dc2275f4ebd132ac1ecb6748aca8d5a27978c423876a384c8d60
lrwxrwxrwx 1 root root   76 Jul 22 10:41 model-00013-of-00033.safetensors -> ../../blobs/6d668f58843bca4e08e03c1ace0bcce04e8d93ed1b05d3dee694b0bc7a839d60
lrwxrwxrwx 1 root root   76 Jul 22 10:44 model-00014-of-00033.safetensors -> ../../blobs/a53585f31eafa44f6df094069462513ed6c454a2365ad93dd0535befb754a6d9
lrwxrwxrwx 1 root root   76 Jul 22 10:46 model-00015-of-00033.safetensors -> ../../blobs/b0bb4d89a5883b122f33ccf781aa9f8902642c5499f785b7d32614967e6fab13
lrwxrwxrwx 1 root root   76 Jul 22 10:48 model-00016-of-00033.safetensors -> ../../blobs/d1dda56c3cdf0a93e2ca259a8c1829cb75537a802f2c6d7caab78ce70ee8d196
lrwxrwxrwx 1 root root   76 Jul 22 10:50 model-00017-of-00033.safetensors -> ../../blobs/0339d1329613e1e472ea2efb23d4ee52b19d01979ef1c12684d2306ee5fb15c1
lrwxrwxrwx 1 root root   76 Jul 22 10:52 model-00018-of-00033.safetensors -> ../../blobs/094988fede9f435ce6ce9f008a1be284237b6e4ac1873c765699ed99246b3613
lrwxrwxrwx 1 root root   76 Jul 22 10:53 model-00019-of-00033.safetensors -> ../../blobs/da03cc20b848da5c96b6876933cf6580efd798c54041acb4295b7d48472916b8
lrwxrwxrwx 1 root root   76 Jul 22 10:54 model-00020-of-00033.safetensors -> ../../blobs/f2816c70c72bb9e3537f7c0d58b98dacec4667713484283feb70ae0ac330d0a5
lrwxrwxrwx 1 root root   76 Jul 22 10:55 model-00021-of-00033.safetensors -> ../../blobs/962d9a18b121f1c8fddd539c3867e0eb06d78a7bdf7716836f0e92d6c4399178
lrwxrwxrwx 1 root root   76 Jul 22 10:57 model-00022-of-00033.safetensors -> ../../blobs/a55ed9a9129177704eca72cf072aa8c840c36d400f1d21ab5d2f813ada9a5092
lrwxrwxrwx 1 root root   76 Jul 22 10:58 model-00023-of-00033.safetensors -> ../../blobs/d85a9570c89353db1a4cce612ff2d49a095ee5939711104959431dcb4fb6d6dc
lrwxrwxrwx 1 root root   76 Jul 22 11:00 model-00024-of-00033.safetensors -> ../../blobs/9acd1ccb69b3359297dd344b1cecd8092f6d4951e0ff81a6b69b579d3a258c78
lrwxrwxrwx 1 root root   76 Jul 22 11:02 model-00025-of-00033.safetensors -> ../../blobs/34a2f5f76356bc9ccbcad7b9dd5383fe1e77607e72992cf6c7fdc400d1ed6487
lrwxrwxrwx 1 root root   76 Jul 22 11:04 model-00026-of-00033.safetensors -> ../../blobs/cb7bac10ce2c1dbcb79e989605cf2600f0e9414c7c6b327fa8207647c28c7377
lrwxrwxrwx 1 root root   76 Jul 22 11:05 model-00027-of-00033.safetensors -> ../../blobs/d0260aea0d19a30142d61185abd96caea14bd0bd3bebf8995657300f0fcd9d6b
lrwxrwxrwx 1 root root   76 Jul 22 11:06 model-00028-of-00033.safetensors -> ../../blobs/9ed43307471db6a6414c3e6cd744f78e2b1bce797d42eccd9455bf0e067972a8
lrwxrwxrwx 1 root root   76 Jul 22 11:07 model-00029-of-00033.safetensors -> ../../blobs/2e16f56179b645f582b153ea20426ce8903dd6b6fb6d67dd14a0b6059e87be3e
lrwxrwxrwx 1 root root   76 Jul 22 11:09 model-00030-of-00033.safetensors -> ../../blobs/93197fbc271271458a29c435fd54f2073b2aa5c4ab2c0015e61d6e50911c5f54
lrwxrwxrwx 1 root root   76 Jul 22 11:10 model-00031-of-00033.safetensors -> ../../blobs/e69dc62861fb64d4426f31eabbd5d441cf1c7c5e93227844090fdabc757c8316
lrwxrwxrwx 1 root root   76 Jul 22 11:12 model-00032-of-00033.safetensors -> ../../blobs/665c87a6610f3015e9e0cec49b1d18a63b266528c8bd6387fd8670c18a2dc92a
lrwxrwxrwx 1 root root   76 Jul 22 11:13 model-00033-of-00033.safetensors -> ../../blobs/d58c375c9e062ef1c203fdd44291b7905276f1ebfbcdc70975c9ca5bb3a789b1
lrwxrwxrwx 1 root root   52 Jul 22 11:13 model.safetensors.index.json -> ../../blobs/677398ef91d675e2052367db8b82666f35d46a36
lrwxrwxrwx 1 root root   52 Jul 22 10:06 preprocessor_config.json -> ../../blobs/dfd7e50d9d4e67cd679b16b337b419a0c6cfa849
lrwxrwxrwx 1 root root   52 Jul 22 10:06 special_tokens_map.json -> ../../blobs/ac23c0aaa2434523c494330aeb79c58395378103
lrwxrwxrwx 1 root root   52 Jul 22 10:06 tokenizer.json -> ../../blobs/1b4f039248a420730cd195d6d6a1a9cc713a7f14
lrwxrwxrwx 1 root root   52 Jul 22 10:06 tokenizer_config.json -> ../../blobs/77b7446cccf72042b1c41dbacb8eb603afe68eca
lrwxrwxrwx 1 root root   52 Jul 22 10:06 vocab.json -> ../../blobs/6bce3a0a3866c4791a74d83d78f6824c3af64ec3
```
* 가중치 파일 갯수
```
find /opt/dlami/nvme/hf-cache/hub -name "*.safetensors" | wc -l
```

### S3 업로드 ###

모델 정보를 저장하는 hf-cache 디렉토리 구조는 아래와 같다.
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

앞에서 얘기한 S3 백업은 hub/ 통째로 올리면 된다. 심볼릭 링크 구조까지 유지하려면 aws s3 sync가 링크를 따라가서 실제 내용을 올려준다.
```
aws s3 sync /opt/dlami/nvme/hf-cache/ s3://${BUCKET}/hf-cache/
```
다음에 복원하면 같은 hub/ 구조로 내려받아지고, HF_HOME이나 캐시 마운트만 맞으면 vLLM이 재다운로드 없이 바로 인식한다.


