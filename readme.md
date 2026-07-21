
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
