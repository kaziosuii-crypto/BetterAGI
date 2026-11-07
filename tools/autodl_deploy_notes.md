# AutoDL Lumine 部署记录（2026-10-27）

## 机器

- 主机：`connect.nmb1.seetacloud.com`
- SSH 端口：`26687`
- 用户：`root`
- 密码：用户提供，未写入本文件
- GPU：RTX 4090 24GB，驱动 560.35.03，CUDA 12.4
- 系统盘 30GB，数据盘 `/root/autodl-tmp` 50GB

## 部署位置

```text
/root/autodl-tmp/
    models/Lumine-Agent-Pretrain-VL-7B/    # 16.5GB，sha256 已核对
    lumine/                                # 服务脚本
        autodl_server.py
        lumine_protocol.py                 # 含解析容错
        serve_lumine.sh
        start_lumine.sh
        qwen2_vl.py.patched                # vLLM 权重映射补丁备份
        requirements-deployed.txt
    lumine_token.txt                       # 600 权限，与本地 tools/lumine_token.txt 一致
```

## 版本

| 组件 | 版本 |
|---|---|
| Python | 3.12.3（`/root/miniconda3`） |
| torch / torchaudio | 2.5.1+cu124 |
| vLLM | 0.7.3 |
| transformers | 4.57.3 |
| tokenizers | 0.21.0 |
| numpy | 1.26.4 |
| huggingface_hub | 0.29.0 |

模型 `model-00001-of-00001.safetensors` 的 SHA256：
`685e3343c57cca1a757d2a9e4e3cfa237c230524141d166cf60ee7815e7d3829`
（与 HF LFS oid 一致）

## 为跑通做的两处兼容

1. **vLLM Qwen2-VL 权重映射**：模型由 transformers 4.57 保存，权重名是
   `model.language_model.*` / `model.visual.*`；vLLM 0.7.3 只认旧版
   `model.*`。已给 `vllm/model_executor/models/qwen2_vl.py` 的
   `hf_to_vllm_mapper` 增加两个更具体的前缀映射。备份见
   `lumine/qwen2_vl.py.patched`。
2. **动作解析容错**：vLLM 有时会丢掉 `<|action_start|>` 特殊 token，
   或省略为 0 的滚轮值、输出空键块。`lumine_protocol.py` 已容错：
   - 缺起始标记时自动补；
   - 两个数字的移动前缀按 `dX dY 0`；
   - 空前缀按原地不动；
   - 重复的 `<|action_end|>` 前缀先剥掉。

## 启动 / 停止

```bash
# 启动（在 AutoDL 上）
screen -dmS lumine bash /root/autodl-tmp/lumine/start_lumine.sh
screen -ls

# 健康检查
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8010/health
curl http://127.0.0.1:8010/status

# 停止
screen -S lumine -X quit
ps aux | grep -E "vllm.entrypoints|autodl_server" | grep -v grep | awk '{print $2}' | xargs -r kill
```

本机接回（SSH 隧道）：

```bash
ssh -N -L 8010:127.0.0.1:8010 -p 26687 root@connect.nmb1.seetacloud.com
```

然后把 `bridge_config.json` 的 `lumineWs` 设为
`ws://127.0.0.1:8010/ws/player`，`lumineToken` 填
`E:\BetterAGI\tools\lumine_token.txt` 的内容。

## 实测

| 项目 | 结果 |
|---|---|
| vLLM 加载 | 约 23 秒（含 CUDA graph） |
| 第一帧（冷） | 约 2.2 秒 |
| 后续帧 | 约 0.39–0.46 秒（约 2.2–2.5Hz） |
| 显存占用 | 21.3GB / 24.5GB |
| 真实游戏截图 | 能输出合法动作块（示例 `-30 0 0 ; ...`） |

5Hz 的协议目标没有达到；0.4 秒一帧意味着动作之间会有约 200ms 空档。
可选的后续优化：更新 vLLM、AWQ 量化、继续降低 `max_pixels`、或在客户端
重复上一动作填缝。当前配置优先保证模型正确加载与动作格式稳定。

## 机器状态

部署完成并验证后，服务已停止，实例已关机，等待用户安装原神后再开机。
数据盘内容与 `start_lumine.sh` 会保留。
