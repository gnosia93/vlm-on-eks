### 1. 아키텍처 개요 ###
### 2. 소스 코드 (config / s3io / inference / worker / merge) ###




### 3. 컨테이너 이미지 빌드 ###

* Dockerfile (s5cmd 추가)
```
FROM vllm/vllm-openai:v0.6.6.post1

WORKDIR /app

# s5cmd (S3 고속 병렬 전송) 설치
RUN curl -sL https://github.com/peak/s5cmd/releases/download/v2.2.2/s5cmd_2.2.2_Linux-64bit.tar.gz \
      | tar -xz -C /usr/local/bin s5cmd \
    && pip install --no-cache-dir pillow==11.0.0 requests==2.32.3 boto3==1.35.76

COPY src/ /app/src/
# ENV PYTHONPATH 는 파이썬이 모듈을 import 할때 뒤지는 경로 목록에 추가하는 설정.
ENV PYTHONPATH=/app/src

ENTRYPOINT []
CMD ["python", "/app/src/run_worker.py"]
```

vllm/vllm-openai:v0.6.6.post1 이미지에는 CUDA 유저스페이스 런타임과 cuDNN, PyTorch, vLLM, 그리고 번들된 NCCL을 비롯해 실행에 필요한 Python 의존성이 모두 포함되어 있다. 반면 EFA 스택은 들어있지 않아서 libfabric, aws-ofi-nccl 플러그인, EFA 커널 드라이버는 별도로 준비해야 하며, NVIDIA GPU 드라이버 역시 컨테이너에는 없고 호스트(노드)에 설치된 드라이버를 nvidia-container-runtime을 사용한다. 따라서 GPU를 쓰려면 GPU 지원 AMI와 NVIDIA device plugin이 노드에 미리 설치되어 있어야 하는데, EKS GPU 노드그룹을 사용한다면 이 부분은 기본으로 제공된다.


### 4. 쿠버네티스 배포 (ConfigMap / Job / Merge Job) ###

Indexed Job은 completion 인덱스마다 새 파드가 뜨는데, 78B는 로딩만 몇 분씩 걸린다. 
g7e에서 TP=4 파드 2대 동시 실행 구조로 completions를 잘게 쪼개지 않고 2로 설정한다. completions=8, parallelism=2로 설정하면 파드가 샤드 하나 끝낼 때마다 새 파드가 뜨면서 모델을 매번 다시 로드하기 때문이다. 그래서 completions=2 = parallelism=2 = NUM_SHARDS=2로 맞춰, 각 파드가 딱 한 번 로드하고 자기 절반을 끝까지 처리하게 한다. 
파드가 죽더라도 resume 로직이 이어서 처리하게 된다. 
가중치는 S3 에 저장한 후 initContainer를 이용하여 파드 실행시 S3 로 부터 로컬 NVMe(s5cmd)로 복사한다.

* vlm-batch-config.yaml
```
apiVersion: v1
kind: ConfigMap
metadata:
  name: vlm-batch-config
  namespace: vlm-batch
data:
  # 로컬 NVMe로 동기화된 가중치 경로에서 로드
  MODEL: "/models/InternVL3-78B"
  MODEL_S3_URI: "s3://my-vlm-data-bucket/models/InternVL3-78B/"
  TENSOR_PARALLEL_SIZE: "4"        # g7e 4-GPU → TP=4
  MAX_MODEL_LEN: "16384"           # 96GB/GPU라 컨텍스트 여유 있음
  GPU_MEMORY_UTILIZATION: "0.92"
  MAX_IMAGES_PER_PROMPT: "1"
  MAX_DYNAMIC_PATCH: "12"
  DTYPE: "bfloat16"
  TEMPERATURE: "0.2"
  TOP_P: "0.9"
  MAX_TOKENS: "1024"
  SEED: "0"

  S3_BUCKET: "my-vlm-data-bucket"
  INPUT_MANIFEST_KEY: "input/manifest.jsonl"
  IMAGE_PREFIX: "input/images/"
  OUTPUT_PREFIX: "output/run-2026-07-22/"
  AWS_REGION: "ap-northeast-2"

  SCRATCH_DIR: "/scratch"
  WRITE_BATCH_SIZE: "48"           # 메모리 여유가 커서 배치 키움
  UPLOAD_EVERY: "128"
  SYSTEM_PROMPT: "당신은 이미지를 정확하고 사실에 근거해 설명하는 어시스턴트입니다."
  DEFAULT_PROMPT: "이미지를 한국어로 자세히 설명해줘."
```

* vlm-batch-infer.yaml
```
# InternVL3-78B 배치 인퍼런스: g7e 4-GPU 노드 2대에서 TP=4 파드 2개 동시 실행.
apiVersion: batch/v1
kind: Job
metadata:
  name: vlm-batch-infer
  namespace: vlm-batch
spec:
  completions: 2            # NUM_SHARDS와 일치 (모델 재로딩 방지 위해 파드=샤드)
  parallelism: 2            # 2대 동시 실행
  completionMode: Indexed
  backoffLimit: 8           # 실패 시 재시도 (resume으로 이어서 처리)
  template:
    metadata:
      labels:
        app: vlm-batch-infer
    spec:
      restartPolicy: Never
      serviceAccountName: vlm-batch-sa      # IRSA로 S3 권한
      # g7e 4-GPU 노드에만 스케줄. 파드당 노드 하나를 통째로 씀.
      nodeSelector:
        node.kubernetes.io/instance-type: g7e.24xlarge
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      # 두 파드를 서로 다른 노드에 분산 (노드당 파드 1개)
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: vlm-batch-infer
              topologyKey: kubernetes.io/hostname

      # 시작 시 S3 가중치를 로컬 NVMe로 병렬 다운로드 (EFS 대신)
      initContainers:
        - name: fetch-weights
          image: YOUR_REGISTRY/vllm-batch-inference:latest   # <-- 교체
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              echo "syncing weights from ${MODEL_S3_URI} ..."
              s5cmd sync "${MODEL_S3_URI}*" /models/InternVL3-78B/
              echo "done."
          envFrom:
            - configMapRef:
                name: vlm-batch-config
          volumeMounts:
            - name: model-local
              mountPath: /models

      containers:
        - name: worker
          image: YOUR_REGISTRY/vllm-batch-inference:latest   # <-- 교체
          imagePullPolicy: IfNotPresent
          command: ["python", "/app/src/run_worker.py"]
          envFrom:
            - configMapRef:
                name: vlm-batch-config
          env:
            - name: NUM_SHARDS
              value: "2"            # completions와 동일
          resources:
            limits:
              nvidia.com/gpu: 4     # TENSOR_PARALLEL_SIZE와 반드시 일치
            requests:
              cpu: "24"
              memory: 200Gi
          volumeMounts:
            - name: model-local
              mountPath: /models      # 로컬 NVMe의 가중치
            - name: scratch
              mountPath: /scratch      # 로컬 임시 결과
            - name: dshm
              mountPath: /dev/shm
      volumes:
        - name: model-local
          emptyDir: {}          # 노드 로컬 NVMe (ephemeral storage가 NVMe여야 함)
        - name: scratch
          emptyDir: {}
        - name: dshm
          emptyDir:
            medium: Memory
            sizeLimit: 24Gi     # TP=4 프로세스 간 통신용
```

#### 이 구성의 동작 요약 ####
* 노드: g7e.24xlarge(96GB×4) 2대. podAntiAffinity로 파드를 한 대씩 분산.
* 각 파드: initContainer가 S3 가중치를 로컬 NVMe로 sync → worker가 TP=4로 한 번만 로드 → 전체 데이터의 절반(인터리브 샤드)을 배치 처리 → 128건마다 S3에 결과 업로드.
* 완료 후 merge-job.yaml 실행 → s3://.../output/run-.../train.jsonl.
* 한 가지 확인할 점: emptyDir가 로컬 NVMe에 잡히려면 해당 노드그룹의 kubelet ephemeral storage가 NVMe 인스턴스 스토어에 마운트돼 있어야 합니다. AMI/노드그룹에서 NVMe를 자동 마운트하지 않는 경우엔 hostPath로 NVMe 마운트 경로(예: /mnt/nvme)를 직접 물리는 방식으로 바꾸면 됩니다. 노드그룹이 NVMe를 어떻게 잡고 있는지 알려주시면 그 부분까지 맞춰드릴게요.

```
또 하나, completions=2면 낙오자(한 파드만 늦게 끝나는 경우) 대응이 거칠어질 수 있어요. 데이터가 아주 크고 처리 시간이 길다면, 모델 재로딩을 감수하고 completions를 늘리는 대신 가중치를 로컬에 미리 받아두고 로딩을 캐시하는 절충도 가능한데, 필요하면 그 방식도 설명해드릴게요.
```

#### 배포하기 ####
```
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/serviceaccount.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/job.yaml
```

### 5. 실행 및 결과 확인 ###
