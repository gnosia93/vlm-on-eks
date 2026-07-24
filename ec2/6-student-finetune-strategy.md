## Student 모델 평가 및 파인튜닝 전략 (Full vs. LoRA) ##

VLM이 프레임을 이해하려면 ① 이미지에서 특징을 뽑는 비전 인코더(InternViT), ② 이미지·언어 토큰을 같은 공간에 대응시키는 비전-언어 정렬(alignment), ③ 이해한
내용을 문장으로 만드는 언어 생성 능력이 필요하다. 정렬이 특히 중요한데, 이것이 돼 있어야 프레임을 넣었을 때 말이 되는 답이 나온다.
우리 태스크는 프레임 16장을 이어 붙인 "영상"이라 여기에 더해 여러 프레임을 시간 순서로 엮어 이해하는 능력도 필요하다. 하지만 InternVL3-1B는 태생이 이미지
VLM이므로, 이 부분은 zero-shot으로 직접 확인해봐야 한다.

이 zero-shot 측정 결과에 따라 파인튜닝 전략을 정하는데, 기본 능력이 어느 정도 갖춰져 있고 시간적 이해만 보완하면 되는 수준이라면 LoRA로 가볍게 튜닝하고,
정렬이나 시간적 이해가 크게 부족해 모델을 폭넓게 재학습해야 한다면 Full 파인튜닝을 선택한다.

### 1. zero-shot 테스트 ###

ubuntu GPU 인스턴스에서 zero-shot 테스트를 수행한다. 
```
echo -e "\n-------------------------------------"
echo "BUCKET: [ $BUCKET ]"
echo "VIDEO_ID: [ $VIDEO_ID ]"

aws s3 sync s3://${BUCKET}/models/internvl3-1b/ /opt/dlami/nvme/hf-cache/models/internvl3-1b/

cd ~/vlm-distillation/src

docker run --rm -it --gpus 1 --shm-size=16g \
  -v $(pwd):/work -w /work \
  -v /opt/dlami/nvme/hf-cache/models:/models \
  -e PYTHONUNBUFFERED=1 \
  -e BUCKET="$BUCKET" \
  --entrypoint python3 \
  vllm/vllm-openai:v0.6.6.post1 \
  zeroshot_eval.py "$VIDEO_ID"
```

[결과]
```
============================================================
[video_id] 09buIj5Z5lk  (zero-shot, /models/internvl3-1b)
------------------------------------------------------------
[caption] 이 프레임들에 무엇이 보이는지 한국어로 설명해줘.
→ 인구가 줄어들고 경기장에서 경기 진행에 대한 정보가 표시된 TV 시리즈의 텍스트가 등장합니다. 캠프는 14.5초 전에 113/1을 기록한 것으로 보인다. 경기장에는 경기장의 레이블이 높은 레이블이 부착되어 있으며, 경기장의 레이블은 "WILLS"로 표시되어 있습니다. 경기장에는 경기장의 레이블이 높은 레이블이 부착되어 있으며, 경기장의 레이블은 "WILLS"로 표시되어 있습니다.
------------------------------------------------------------
[temporal] 프레임 순서대로 장면이 어떻게 변하는지 시간 순서대로 한국어로 설명해줘.
→ The video starts with a scene of a bowler delivering a ball to a batsman, who is wearing a light blue uniform.
The bowler is in a green uniform, and the batsman is in a green and yellow uniform.
The scoreboard shows 'PAK 104/1' with 'Target 288 (40)' and 'Overs 14'. The batsman hits the ball,
and the ball is seen flying towards the boundary. The next scene shows the ball hitting the boundary,
and the batsman is seen running towards the wicketkeeper.
The scoreboard updates to 'PAK 107/1' with 'Target 288 (40)' and 'Overs 14.2'.
The scene then shifts to a different player in a light blue uniform standing near the boundary,
with the scoreboard showing 'PAK 113/1' and 'Target 288 (40)'.
The final scene of the first clip shows a player in a green uniform standing near the boundary,
with the scoreboard showing 'PAK 113/2' and 'Target 288 (40)'.
The video concludes with a group of players celebrating on the field.
------------------------------------------------------------
[action] 영상 속 인물이 어떤 동작을 하고 있는지 한국어로 알려줘.
→ The batsman is standing and looking at the ball.
------------------------------------------------------------
[sport] 이 영상의 스포츠 종목을 한 단어로만 답해줘.
→ cricket
============================================================
```

화면에 프롬프트 4개(caption/temporal/action/sport)의 응답이 출력됩니다. 
특히 temporal 응답을 teacher(78B)의 크리켓 설명과 비교해보면 1B의 시간 이해 수준을 파악할 수 있습니다.

### s3_infer.py(teacher, 78B)의 응답과 비교 ###
```
이 영상은 크리켓 경기의 한 장면을 보여줍니다. 파키스탄이 104/1로 경기를 진행 중이며, 목표점은 288점입니다. 투수는 라즈 아흐메드가 등판하고 있습니다.
타자는 공을 치고 뛰기 시작하지만, 수비수들이 빠르게 반응하여 아웃을 성공합니다. 이후 다른 타자가 등장하여 공을 치지만, 또다시 아웃됩니다. 팀원들이
기뻐하며 축하합니다.
```
(video_id: 09buIj5Z5lk, s3://.../inference/042dd539417d.json에 저장된 것)

#### teacher가 보여준 능력 (78B) ####
- 비전+정렬: 크리켓 경기임을 정확히 파악, 스코어보드(104/1, 목표 288)까지 읽음
- 시간 이해: "공을 친다 → 뛴다 → 아웃 → 다른 타자 등장 → 또 아웃 → 축하" 로 프레임 간 사건 순서를 엮음 ← 이게 핵심
- 언어 생성: 자연스러운 한국어 서술


### 파인튜닝 전략 (Full vs. LoRA) 결정 ###

**_아래는 LLM Judge 로 teach / student 응답을 비교한 결과이다_**

#### 1. 능력별 진단 (student 1B, zero-shot) ####

| 능력 | 결과 | 판정 |
| :--- | :--- | :---: |
| **비전 인코더** | 스코어보드 `PAK 104/1`, `Target 288`, `Overs 14`, `WILLS` 광고까지 읽음 | ✅ **매우 우수** |
| **정렬(alignment)** | 크리켓 경기·유니폼·투수/타자 정확히 대응 | ✅ **우수** |
| **시간 이해** | `"투구 → 타격 → 공이 바운더리로 → 107/1 → 113/1 → 113/2 → 축하"`<br>사건 순서를 프레임 넘어 엮음 | ✅ **놀랍게도 됨** |
| **분류(sport)** | `cricket` 정답 | ✅ |
| **언어 생성 (한국어)** | ❌ `temporal`·`action`이 영어로 나옴 | ⚠️ **문제** |

#### 2. 핵심 관찰 ####

부족한 건 "이해"가 아니라 "한국어 출력"이에요.

- temporal 응답을 보면 1B가 시간 순서를 이미 잘 이해하고 있어요. 스코어가 104→107→113→113/2로 바뀌는 걸 따라가고, 마지막에 축하까지 엮었죠. teacher와 사건
흐름이 거의 같아요.
- 그런데 한국어로 시켰는데 영어로 답했어요 (caption은 한국어인데 깨졌고, temporal/action은 통째로 영어).
- caption의 한국어는 "인구가 줄어들고... TV 시리즈" 같은 환각+반복이 있어요.

즉 비전·정렬·시간 이해라는 어려운 능력은 이미 갖춰져 있고, 무너진 건 상대적으로 쉬운 "한국어로, 지정된 형식으로 말하기(instruction following + 언어)"
예요.

#### 그래서 → LoRA ####

LoRA를 권하는 이유:

- 고칠 게 "행동/스타일"이지 "지식/능력"이 아님 → 어댑터로 충분
- teacher 출력(크리켓 설명체)을 정답으로 하는 distillation 데이터로 LoRA 튜닝하면, 이미 있는 이해 능력이 한국어 서술로 흘러나오도록 정렬됨
- 저렴/빠르고, 원본 능력(스코어보드 읽기 등)을 안 망가뜨림


만약 Full이 필요했을 시나리오 (대비용):

- student가 크리켓을 야구로 착각하거나, 스코어보드를 못 읽거나, temporal에서 프레임을 뒤섞어 순서가 엉켰다면 → 이해 자체가 안 되는 것 → Full 고려.
- 지금은 그 반대예요. 이해는 하는데 표현만 안 맞음.

## 인스턴스 삭제 ##

> [!WARNING]
> GPU 인스턴스는 가용 수량이 제한적이고 시간당 비용도 비싸다.
> 실습이 끝나면 이 단계에서 **반드시 삭제**해 불필요한 과금을 막는다.

ubuntu GPU 인스턴스에서 exit 명령어를 두번 수행하셔 vs-code ec2 인스턴스로 돌아온 다음 (프롬프트는 x86_64 이다), GPU 인스턴스를 삭제한다. 
```
echo -e "\n-------------------------------------"
echo "INSTANCE: [ $INSTANCE ]"
echo "REGION: [ $REGION ]"

aws ec2 terminate-instances --instance-ids $INSTANCE --region $REGION
```


