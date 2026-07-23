# DDP 통신 토폴로지 정리

## 1. DDP가 통신하는 것 — gradient all-reduce

DDP는 각 GPU에 **모델을 통째로 복제**하고, 각자 다른 데이터 조각으로 forward/backward를 돈다. 통신은 딱 하나, **gradient를 all-reduce**(모든 GPU의 gradient를 합·평균해 다시 배포)뿐이다.

```
   GPU0            GPU1            GPU2            GPU3
 ┌───────┐      ┌───────┐      ┌───────┐      ┌───────┐
 │ model │      │ model │      │ model │      │ model │   ← 모델을 각 GPU에 통째로 복제
 │ (복제)│      │ (복제)│      │ (복제)│      │ (복제)│
 └───┬───┘      └───┬───┘      └───┬───┘      └───┬───┘
  data0          data1          data2          data3      ← 데이터만 나눠서
     │              │              │              │
   forward/backward (각자 독립적으로 gradient 계산)
     │              │              │              │
     └──────────────┴──── all-reduce ────┴──────────────┘  ← 통신은 여기 한 번
                    (gradient 합산·평균 후 재배포)
     │              │              │              │
  optimizer.step()  (모든 GPU가 동일 gradient로 갱신 → 가중치 동기 유지)
```

## 2. 언제 통신하나 — 스텝(optimizer step)마다

- **micro-batch(forward/backward)마다가 아니라, 가중치 갱신 직전에 1번**
- gradient accumulation 중인 micro-batch 사이엔 통신 안 함(`no_sync`, 누적만)
- → **accum을 키우면 스텝이 뜸해져 통신 빈도↓**

## 3. ⭐ 통신량은 "모델 크기"에만 비례 — 배치와 무관

all-reduce하는 gradient의 크기는 **파라미터 수와 같다.** 배치가 1이든 128이든 gradient의 shape은 동일하므로 **통신량은 고정**이다.

```
                 배치 ↑ 키우면?
   ┌─────────────────────────────┬──────────┐
   │ forward/backward 계산량      │  ✅ 증가  │
   │ 활성값(activation) 메모리    │  ✅ 증가  │  ← §4 GPU 메모리 병목
   │ GPU 간 통신량 (all-reduce)   │  ❌ 그대로│  ← 모델 크기에만 비례
   └─────────────────────────────┴──────────┘

   통신량 = 파라미터 수 × dtype 크기   (배치 항이 없음!)
     · 1B full  → gradient ~2GB
     · 1B LoRA  → 수십 MB  (학습 대상 1% 미만)
```

오히려 배치를 키우면 같은 데이터셋의 **스텝 수가 줄어** all-reduce 횟수 감소 → **통신 오버헤드는 오히려 줄어든다.**

## 4. ⭐ 노드(GPU)가 늘어도 GPU당 통신량은 상한이 있다

직관과 달리, GPU를 늘려도 **GPU 하나가 주고받는 양은 거의 안 늘어난다.** NCCL의 **ring all-reduce**가 그렇게 설계돼 있다.

$$
\text{GPU당 통신량} = 2 \times \frac{N-1}{N} \times (\text{gradient 크기}) \quad\longrightarrow\quad 2\times\text{gradient 로 수렴}
$$

```
GPU당 통신 배수 = 2(N-1)/N
 N=2   ■■              1.00×
 N=4   ■■■             1.50×
 N=8   ■■■■            1.75×
 N=16  ■■■■            1.88×
 N=∞   ■■■■            2.00×  ← 여기서 멈춤 (GPU 아무리 늘어도 상한)
```

| 관점 | 노드 늘면? |
|------|-----------|
| **GPU 1개당** 통신량 | ❌ 거의 그대로 (2×gradient로 수렴) |
| **시스템 전체** 통신 총량 | ✅ 늘어남 (GPU 수 비례) — 단 각 GPU가 병렬 처리 |
| **통신 지연(latency)** | 🔸 약간 늘어남 (ring 길어져 홉 증가) |

→ 병목은 "GPU 하나가 감당하는 양"인데 그게 상한이 있으니, **GPU를 늘려도 선형에 가깝게 스케일**된다.

## 5. 통신 경로(토폴로지) — 빠른 순 3단계 (노드 내부)

```
 [빠름] ────────────────────────────────────────────► [느림]

  ① NVLink              ② PCIe P2P            ③ 시스템 메모리 경유(bounce)
 GPU◄════════►GPU      GPU◄──PCIe──►GPU        GPU──►[호스트 RAM]──►GPU
  전용 링크             스위치 통해 직접         RAM에 복사 2번(2홉)
  600~900 GB/s          ~32GB/s(Gen4)           PCIe 상한 같으나 지연 큼
  RAM 경유 ❌           RAM 경유 ❌             RAM 경유 ✅
```

- **PCIe P2P ≠ 시스템 메모리 경유**: 둘 다 PCIe 버스를 타지만 P2P는 직접(1홉), bounce는 호스트 RAM 왕복(2홉)
- 클라우드/가상화에선 P2P가 막혀 **SYS(bounce)로 떨어질 수 있음** → `nvidia-smi topo -m`으로 확인
  (`NV#`=NVLink, `PIX/PXB`=PCIe, `SYS`=시스템 경유)

## 6. 멀티노드 — 진짜 신경 쓸 곳은 노드 간 링크

노드가 여러 개면 노드 내부(NVLink/PCIe)는 빠르지만 **node ↔ node는 네트워크**를 탄다.

```
[노드A: GPU0-1-2-3]  ◄══ 네트워크 ══►  [노드B: GPU4-5-6-7]
   NVLink/PCIe (빠름)   EFA / 이더넷        NVLink/PCIe (빠름)
     수백 GB/s          (상대적으로 느림)      수백 GB/s
                    이더넷 25~100Gbps(≈3~12GB/s)
                    AWS EFA 면 수백 Gbps
```

- 멀티노드에서 유일하게 신경 쓸 병목은 **노드 간 네트워크**
- 그런데 **1B LoRA → gradient 수십 MB** → 노드 간이 좀 느려도 스텝당 수 ms, 계산(수백 ms~초) 대비 여전히 미미

## 7. 결론 — 1B DDP엔 NVLink도, 고속 인터커넥트도 필수 아님

```
 스텝당 통신 시간 (all-reduce)        vs   스텝당 계산 시간 (16프레임 fwd+bwd)
 ─────────────────────────────            ─────────────────────────────────
 1B full  : ~2GB / PCIe ≈ 수십 ms         수백 ms ~ 초 단위
 1B LoRA  : 수십 MB / PCIe ≈ 1~2 ms
                    └─► 통신 비중 한 자릿수 %, LoRA면 1% 미만
```

- 통신량이 작고(모델 크기에만 비례), GPU당 부담엔 상한(2×gradient)이 있어 → **PCIe로 충분, NVLink는 과투자**
- **`g5`(A10G, NVLink 없음) + 노드 수 확장**으로 선형에 가깝게 스케일
- 노드 간도 **1B LoRA면 일반 네트워크로 충분**, EFA 같은 고속 인터커넥트도 필수는 아님
- NVLink·EFA가 값하는 건 **FSDP/TP로 모델을 쪼개거나, full fine-tune + 수십 노드**로 갈 때
- (별개) **`/dev/shm`은 DataLoader worker용** — GPU 통신과 무관하지만 16프레임 텐서라 부족하면 학습이 죽음. num_workers 기준 2~8GB 확보

---

**핵심 한 줄:** DDP 통신량은 **모델 크기에만 비례**(배치·GPU 수 무관, GPU당 2×gradient 상한)하므로, 1B LoRA에서는 스텝마다 소량 gradient만 오가 **PCIe·일반 네트워크로도 선형 스케일**되고 NVLink·EFA는 필요 없다.
