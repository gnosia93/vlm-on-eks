# vlm-distillation

본 워크샵에서는 70B+ 급 이상의 대규모 Vision-Language Model(VLM)을 teacher 모델로 활용해, 영상·이미지를 분석하고 1B 급의 경량 증류(distilled) 모델을 구축하는 방법을 다룹니다.
전체 ML 파이프라인은 다음 흐름으로 구성됩니다. 

![](https://github.com/gnosia93/vlm-distillation/blob/main/images/distillation-flow-1.png)

대규모 VLM을 상시 서빙하는 대신, 배치 인퍼런스로 학습 데이터를 생성해 경량 모델로 증류함으로써 추론 비용을 크게 낮추는 것이 핵심입니다. 또한 CPU 중심 작업(영상 샘플링)은 Graviton에, GPU 집약 작업(VLM 인퍼런스·학습)은 GPU 노드풀에 배치해 워크로드별로 최적의 리소스를 사용합니다.

teacher 모델로는 InternVL3-78B를, student 모델로는 같은 계열의 InternVL3-1B를 사용합니다. teacher와 student를 동일한 InternVL3 계열로 맞춘 이유는, 이미지 전처리 방식과 프롬프트·출력 포맷, 토크나이저가 모두 일관되어 데이터 생성부터 학습까지 하나의 전처리 파이프라인으로 관리할 수 있기 때문입니다. 이는 서로 다른 모델 계열을 조합할 때 발생하는 전처리·템플릿의 이중 관리 부담을 없애 워크샵의 흐름을 단순하게 유지해 줍니다.

학습 데이터로는 Hugging Face가 공개한 대규모 영상 이해 데이터셋 FineVideo를 사용합니다. CC-BY 라이선스의 실사 영상 약 43,000개(총 약 3,425시간, 122개 카테고리)로 구성되며, 각 영상에는 장면·등장인물·활동·상호작용·분위기·내러티브가 타임코드 단위로 주석되어 있습니다. 이는 인물의 행동과 상호작용을 분석하려는 본 파이프라인의 목적에 잘 맞습니다. 또한 시간 코드가 포함된 **음성 전사(transcript)**가 제공되어, 프레임만으로 판단하기 어려운 "대화 여부"를 음성 정보로 보완할 수 있습니다.


## Table of Contents ##

먼저 EC2 단일 노드에서 파이프라인 전 과정을 소규모로 검증한 뒤, 동일한 워크플로우를 EKS에서 병렬로 스케일아웃 합니다.

### ■ _Part 1. Basic (EC2 단일 노드 검증)_ ###

* [1. 기반 인프라 구축 - VPC부터 EKS 클러스터까지](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/1-vpc-create.md)
* [2. 데이터셋 / 모델 가중치 다운로드](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/2-preparing-datasets.md)
* [3. 프롬프트 설계 및 출력 스키마 확정](https://github.com/gnosia93/vlm-distillation/blob/main/ec2/3-prompt-design.md)
* [4. 프레임 샘플링](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/4-frame-sampling.md)
* [5. InternVL3-78B 기반 학습 데이터 생성 (영상 자동 라벨링)](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/5-vlm-infer.md)
* [6. Student 모델 평가 및 파인튜닝 전략 (Full vs. LoRA)](https://github.com/gnosia93/vlm-distillation/blob/main/ec2/6-student-finetune-strategy.md)
* [7. Student 모델 파인튜닝](https://github.com/gnosia93/vlm-on-aws/blob/main/ec2/6-student-finetune.md)
* [8. 파인튜닝 모델 평가]
  
### ■ _Part 2. Advanced (EKS 병렬 스케일아웃)_ ###

* [1. 워크플로우 컨테이너화 (Docker Image 빌드 & ECR 푸시)]
* [2. K8s Job 기반 증류 학습 데이터 병렬 생성 (VLM 대규모 배치 인퍼런스)](https://github.com/gnosia93/vlm-on-eks/blob/main/labs/5-vlm-infer.md) — 다수의 Job으로 VLM 배치 인퍼런스를 병렬 수행해 Student의 학습용 데이터를 생성.
* [3. PyTorch DDP 기반 Student 모델 분산 학습]


### ■ _Wrap Up_ ###

* [1. 리소스 정리](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/8-delete-resource.md)

## Appendix ##

* [PyTorch DDP 통신 토폴로지 및 멀티 노드 네트워크 최적화](https://github.com/gnosia93/vlm-distillation/blob/main/appendix/ddp-communication-topology.md)
* [PyTorch를 사용하여 처음부터 VLM(비전 언어 모델)을 구현하고 학습시키기](https://www.youtube.com/watch?v=lY8vmKrFVew)
