# MiniCPM-o 4.5 实时屏幕解说系统 (new-structure)

本目录为重新设计的轻量级实时屏幕解说模块，直接使用 **MiniCPM-o-4.5-Realtime** 大模型全双工盯住当前电脑屏幕或指定游戏窗口进行实时流式解说。

---

## 模块结构

```text
new-structure/
├── live_commentator.py     # 解说主程序（持续截帧、调用大模型、流式输出与日志记录）
├── minicpm_realtime.py     # ModelBest 官方 WebSocket 实时双工客户端封装
├── screen_capture.py       # 高性能 Windows GDI 屏幕与窗口截帧模块
├── start_commentary.bat    # 一键启动脚本（双击即用）
└── README.md               # 模块说明文档
```

---

## 一键启动

在 `new-structure/` 目录下直接双击运行 **`start_commentary.bat`**：
* 直接按回车：开始盯住整个电脑桌面进行实况解说；
* 输入窗口关键词（如：`原神` 或 `UNDERTALE`）：自动锁定对应窗口进行解说。

---

## 命令行启动

```bash
cd new-structure

# 模式 1: 盯住电脑全屏
python live_commentator.py

# 模式 2: 盯住指定窗口
python live_commentator.py --window 原神

# 更多参数
python live_commentator.py --window 原神 --interval 2.5 --max-tokens 120
```

---

## API 凭据配置

模型连接基于 ModelBest Realtime API：
* 默认端点：`wss://api.modelbest.cn/v1/realtime?mode=chat&model=MiniCPM-o-4.5-Realtime`
* API Key 自动查找优先级：
  1. 命令行参数 `--key <YOUR_KEY>`
  2. 环境变量 `MINICPM_API_KEY`
  3. `keys/minicpm.key`
