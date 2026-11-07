# 离线/实时 SVC 变声引擎

本目录是独立于 Amaidesu 与 BetterGI 的 so-vits-svc 推理环境，供后期成片
把 SAPI 基础语音转换成角色音色。**不包含任何模型权重**，模型需自备。

## 环境

| 组件 | 版本 |
|---|---|
| Python | 3.12.14 |
| torch | 2.9.1+cu128 |
| torchaudio | 2.9.1+cu128 |
| so-vits-svc-fork | 4.2.30 |
| GPU | RTX 5070 Laptop 8GB，compute capability 12.0 |

已实测 `torch.cuda.is_available() == True`、capability `(12, 0)`、GPU 矩阵
乘法正常，黑韦尔（sm_120）内核可用。

解释器：`E:\BetterAGI\svc\.venv\Scripts\python.exe`
命令行：`E:\BetterAGI\svc\.venv\Scripts\svc.exe`

## 模型格式

so-vits-svc 4.0 / 4.1 的模型目录，至少包含：

```text
E:\BetterAGI\svc\models\<模型名>\
    G_<步数>.pth     # 生成器权重（目录里有多个时取最新修改的）
    config.json      # 训练配置，必须与 pth 配套
```

可选：

* `cluster/*.pt` + `-k`：特征检索，音色相似度更高；
* 多说话人模型的 `-s <speaker>`；
* 变调 `-t <半音>`（男声转女声常用 +12）。

RVC 的 `.pth + .index` 与本引擎不兼容；如果只有 RVC 模型，需要另装 RVC
推理端（届时把 `render_session.py --svc-cmd` 换成对应命令即可）。

## 用法

单句转换：

```bash
E:\BetterAGI\svc\.venv\Scripts\python.exe -m so_vits_svc_fork infer input.wav \
    -m E:\BetterAGI\svc\models\<模型名> \
    -c E:\BetterAGI\svc\models\<模型名>\config.json \
    -o output.wav
```

质量与显存：

* `-fm crepe` 比默认 `dio` 更准，速度慢一些；离线成片建议用 `crepe`；
* 显存不足时降低 `-mc`（单块最大秒数），或 `-d cpu`（很慢）；
* 8GB 显存跑 4.1 模型通常占用 1–2GB，足够。

实时模式（麦克风输入，供试听）：

```bash
E:\BetterAGI\svc\.venv\Scripts\svc.exe gui
E:\BetterAGI\svc\.venv\Scripts\svc.exe vc
```

## 与成片流水线对接

`BetterGI/integrations/session/render_session.py` 的默认命令已经指向本环境：

```bash
python render_session.py sessions/<会话id> --tts sapi \
    --svc-model E:/BetterAGI/svc/models/<模型名>
```

渲染器会逐句执行 `infer`，把结果按 `session.jsonl` 的 `t` / `dur` 对齐。
SVC 失败时默认整体报错；加 `--allow-tts-fallback` 可回退到 SAPI 基础音。

## 许可证

* so-vits-svc-fork：MIT；
* PyTorch：BSD 风格；
* 模型权重由用户提供，其许可证与使用范围由模型作者决定。

本目录不下载、不分发任何角色音色模型。
