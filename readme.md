# vlm-distillation

본 워크샵에서는 70B+ 급 이상의 대규모 Vision-Language Model(VLM)을 teacher 모델로 활용해, 영상·이미지를 분석하고 1B+ 급의 경량 정제(distilled) 모델을 구축하는 방법을 다룹니다.
전체 ML 파이프라인은 다음 흐름으로 구성됩니다. 
```
S3 영상 → 전처리 → VLM 인퍼런스 → JSON 라벨 → 검수 → 학습 → 검증
```
대규모 VLM을 상시 서빙하는 대신, 배치 인퍼런스로 학습 데이터를 생성해 경량 모델로 증류함으로써 추론 비용을 크게 낮추는 것이 핵심입니다. 또한 CPU 중심 작업(영상 샘플링)은 Graviton에, GPU 집약 작업(VLM 인퍼런스·학습)은 GPU 노드풀에 배치해 워크로드별로 최적의 리소스를 사용합니다.

teacher 모델로는 InternVL3-78B를, student 모델로는 같은 계열의 InternVL3-1B를 사용합니다. teacher와 student를 동일한 InternVL3 계열로 맞춘 이유는, 이미지 전처리 방식과 프롬프트·출력 포맷, 토크나이저가 모두 일관되어 데이터 생성부터 학습까지 하나의 전처리 파이프라인으로 관리할 수 있기 때문입니다. 이는 서로 다른 모델 계열을 조합할 때 발생하는 전처리·템플릿의 이중 관리 부담을 없애 워크샵의 흐름을 단순하게 유지해 줍니다.

teacher로 78B급을 선택한 것은, 32B급으로는 영상 속 인물의 행동이나 상호작용처럼 미묘한 장면을 충분히 정밀하게 기술하기 어렵고, 235B급 MoE는 이 워크샵 규모에서 서빙 부담이 지나치게 크기 때문입니다. InternVL3 계열은 1B부터 78B까지 사이즈 간격이 촘촘해, teacher와 student의 규모를 목적에 맞게 유연하게 조정할 수 있다는 점도 선택의 이유입니다. student로는 최종 배포 시 추론 비용과 응답 속도가 가장 유리한 1B 모델을 채택하되, 목표 품질에 따라 동일 계열의 2B 모델로 손쉽게 승급할 수 있도록 구성했습니다.

라이선스 측면에서도 InternVL3는 MIT 라이선스로 공개되어 있어 상업적 활용과 파인튜닝, 재배포가 자유롭습니다. 특히 최종 산출물인 student 모델(InternVL3-1B)은 Apache-2.0 기반 백본을 사용하므로, 증류로 얻은 경량 모델을 실제 서비스에 배포하는 데에도 제약이 없습니다.

학습 데이터로 FineVideo를 사용합니다. FineVideo는 Hugging Face가 공개한 대규모 영상 이해용 데이터셋으로, YouTube에 Creative Commons Attribution(CC-BY) 라이선스로 공개된 약 43,000개의 실사 영상(평균 4.7분, 총 약 3,425시간, 122개 카테고리)을 YouTube-Commons로부터 수집해 구성했습니다.

이 데이터셋은 각 영상에 대해 장면(scene)·등장인물·활동·인물 간 상호작용·분위기(mood)·내러티브 흐름을 타임코드 단위로 정밀하게 주석한 것이 특징으로, 화면 속 인물이 무엇을 하고 있는지, 인물 간 대화나 상호작용이 어떻게 이뤄지는지를 분석하려는 본 파이프라인의 목적에 잘 부합합니다. 또한 전체 영상의 speech-to-text 전사(transcript)가 시간 코드와 함께 제공되어, 영상 프레임만으로는 판단하기 어려운 "대화 여부"를 음성 정보로 보완할 수 있습니다.



## 챕터 구성 ##

먼저 EC2 단일 노드에서 파이프라인 전 과정을 소규모로 검증한 뒤, 동일한 워크플로우를 EKS에서 병렬로 스케일아웃 합니다.

### EC2 ###

* [1. VPC 생성하기](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/1-vpc-create.md)
* [2. 데이터셋 / 모델 가중치 다운로드](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/2-preparing-datasets.md)
* [3. 프롬프트 구체화 / 튜닝]
* [4. 영상 프레임 샘플링](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/4-frame-sampling.md)
* [5. InternVL3-78B 기반 학습 데이터 생성 (영상 자동 라벨링)](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/5-vlm-infer.md)
* [6. Student 모델 zero-shot 영상 이해도 테스트](https://github.com/gnosia93/vlm-distillation/blob/main/ec2/6-student-level-test.md)
* [7. Student 모델 파인튜닝](https://github.com/gnosia93/vlm-on-aws/blob/main/ec2/6-student-finetune.md)
* [8. 파인튜닝 모델 평가 / 검증]
* [9. 리소스 삭제](https://github.com/gnosia93/vlm-on-eks/blob/main/ec2/8-delete-resource.md)
  
### EKS ###
* EKS 클러스터 구성 — GPU 노드풀과 Graviton 노드풀을 함께 준비하고, 워크로드별 스케줄링 기반을 마련합니다.
* 데이터셋 준비 및 S3 적재 — FineVideo에서 대상 카테고리의 영상을 내려받아 원본 영상과 메타데이터를 S3에 저장하고, 이후 파이프라인이 참조할 데이터 레이아웃을 구성합니다.
* 프롬프트 설계 및 출력 스키마 확정 — EC2에서 프롬프트를 반복 검증하고, 이후 단계가 의존할 출력 JSON 스키마를 확정합니다.
* 영상 샘플링 (Graviton CPU Job) — S3 원본 영상에서 프레임을 추출해 대상 데이터셋을 구성합니다.
* [vLLM 병렬 인퍼런스 (K8s Job)](https://github.com/gnosia93/vlm-on-eks/blob/main/labs/5-vlm-infer.md) — 다수의 Job으로 VLM 배치 인퍼런스를 병렬 수행해 정제 모델 학습용 데이터를 생성합니다.
* 학습 데이터 검증 및 보정 — 생성된 JSON을 스키마 기준으로 자동 검증하고, 오류·이상 데이터를 걸러 학습셋 품질을 확보합니다.
* 정제 모델 학습 — 확보한 학습 데이터로 1B급 경량 모델을 학습합니다.
* 정제 모델 검증 — 학습된 모델을 teacher 대비 성능 기준으로 평가합니다.

## 부록 ##

* [DDP 의 이해]()
