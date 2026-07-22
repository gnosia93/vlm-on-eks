# vlm-on-eks

이 워크샵을 100B+ 급 영상/이미지를 분석하는 모델을 가지고 1B+ 급 정제 모델을 만드는 방법에 대해 기술한 워크샵입니다.
ML 파이프라인은 S3 영상 ─→ 전처리 ─→ VLM ─→ JSON ─→ 검수 ─→ 학습 ─→ 검증 과정을 거치게 되며, 파이프라인은 EKS 위에서 동작합니다.

* EKS 설치

* 프롬프트 설계 및 검증 /w EC2

* 영상 샘플링 (Graviton CPU JOB) - S3 데이터 원본 및 타겟

* K8S JOB + vLLM 으로 병렬 인퍼런스 -> 정제 모델을 위한 훈련 데이터 확보

* 훈련 데이터 검증 및 수정

* 정제 모델 훈련 및 검증.







----


* mock 데이터 생성
```
brew install ffmpeg-full

ffmpeg -f lavfi -i "testsrc=duration=10:size=640x480:rate=30" \
       -vf "drawtext=text='clip_00042 frame %{n}':fontsize=24:fontcolor=white:x=20:y=20" \
       -pix_fmt yuv420p clip_00042.mp4
```

```
  [워크샵]  ffmpeg 영상 ─→ 전처리·추론 (인프라 증명, JSON은 버림)
            mock JSONL ─────────────→ 학습·검증 (라벨 로직 증명)

  [실제]    영상 ─→ 전처리 ─→ VLM ─→ JSON ─→ 검수 ─→ 학습 ─→ 검증
                                (한 줄로 연결)
```
