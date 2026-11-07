---
name: text_adv_vlm_question
version: "1.0"
description: "text_adv Agent 读屏 VLM 的 question 模板（要求固定可解析格式与选项中心像素坐标）"
variables: [options_directive]
author: Amaidesu
tags: [agent, text_adv, vlm]
---
请识别这张视觉小说游戏画面，并严格按以下固定格式回复，不要添加任何其他内容：
正文：
<画面正文的全部文字，按原始换行>
$options_directive
