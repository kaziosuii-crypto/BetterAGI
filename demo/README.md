# 合成演示

这里是不依赖原神的端到端样例，用来验证「会话文件 → 配音对齐 → 成片」：

```bash
# 1) 8 秒测试画面
ffmpeg -f lavfi -i "testsrc2=size=1280x720:rate=30" -t 8 \
    -c:v libx264 -pix_fmt yuv420p demo/session/gameplay.mp4

# 2) 用 session_format 写入 3 条解说、2 条思考、动作与姿态
#    （脚本见 integrations/session/tests/test_render_session.py）

# 3) 后期渲染
python BetterGI/integrations/session/render_session.py demo/session --tts sapi
```

产物：

* `session/gameplay.mp4`：测试图案画面；
* `session/session.jsonl`：时间线、思考、解说、动作、VTuber 参数；
* `session/final.mp4`：SAPI 中文配音 + 烧录字幕的成片；
* `session/final_feiyu.mp4`：飞鱼 CosyVoice3 `cute` 声线配音的同一场成片；
* `session/final_avatar.mp4`：额外叠加模型展示图（`--avatar-image`），
  只用于验证叠加位置；正式使用请换成透明背景的角色立绘；
* `session/subtitles.ass`、`session/render.json`：字幕与对齐报告。

`final.mp4` 里配音是 Windows SAPI 的基础音色；换成 SVC 模型后声音会变成
角色音色，时间轴与字幕不变。测试图案只用于验证流水线，不代表真实游戏。
