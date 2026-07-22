
### FineVideo 데이터셋의 의해 ###

* 영상 약 43,000개 / 3,400시간 구성되어있고 전체 용량이 수백 GB~TB 정도이다.
* WebDataset 포맷: tar 샤드 안에 .mp4(영상)와 .json(메타데이터)가 쌍으로 들어 있다.
* 각 샘플의 JSON에는 자체 택소노미 기반 카테고리(예: content_parent_category, content_fine_category)와 YouTube 메타데이터가 들어 있다.
* 게이트 데이터셋으로, HF 페이지에서 라이선스(CC) 동의를 먼저 해야 하고, 다운로드 시 HF_TOKEN이 필요하다.

[!IMPORTANT] CC 라이선스 영상이라 재배포/저장 시 원본 라이선스와 저작자 표시(attribution) 조건을 지켜야 하는데, JSON의 provenance 필드를 함께 S3에 저장해두면 나중에 출처 추적이 된다.
