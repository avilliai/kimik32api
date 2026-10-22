# Kimik3 2API (支持免费 DeepSeek V4 Flash)

将 kimik3.net 逆向封装为标准 OpenAI 兼容的 HTTP API 接口服务，支持流式 (SSE) 与非流式对话。

---

## 📌 项目可用性诊断与维护说明

1. **项目可用性检测**：
   - 原项目所有旧账号（使用 `@qq.com`）均已失效，官方已全面封禁 QQ 邮箱注册（返回 `QQ Mail registrations are not accepted`）。
   - 官方上游模型体系已调整：目前 `deepseek-v4-flash` 和 `glm-5.3-flash` 为**完全免费模型 (Free Tier)**；而 `kimi-k3` 已转为付费 (Payment Required)。
   - 对话请求体参数与 SSE 流式返回已适配上游协议更新，默认模型调整为 **`deepseek-v4-flash`**。

2. **双轨邮箱认证与自动补号系统**（参考并对齐 `converseai2api`）：
   - **方式一：Outlook 第一方邮箱池**（优先）：从根目录 `outlook_live_*.txt` 文件循环轮询获取微软第一方账号，自动维护指针文件 `outlook_pointer.json`，100% 绕过邮箱域名拦截。
   - **方式二：Cloudflare Worker 域名临时邮箱**：通过 `.env` 中的 `WORKER_DOMAIN`、`EMAIL_DOMAIN`、`ADMIN_PASSWORD` 动态创建邮箱地址并支持收取邮件验证。
   - **自动号池饱和机制**：常驻号池保持 3 个健康账号，探活失败或额度用尽时自动剔除并补号。
   - **Turnstile 支持**：上游目前未开启验证码；系统已预留 Turnstile Solver（本地 127.0.0.1:5072 或 YesCaptcha）扩展逻辑。

---

## 🤖 支持的模型列表

| 模型名称 | 免费/付费 | 说明 |
| :--- | :--- | :--- |
| **`deepseek-v4-flash`** | **免费 (推荐)** | 默认模型，响应快，长文本与代码能力强 |
| **`glm-5.3-flash`** | **免费** | 智谱免费大模型 |
| `deepseek-chat` / `deepseek-v4` | 免费 | 自动映射到 `deepseek-v4-flash` |
| `kimi-k3` | 付费 | 上游默认模型（需有 balance） |
| `gpt-3.5-turbo` / `gpt-4` | 兼容 | 自动映射兼容常用客户端 |

---

## ⚙️ 环境变量配置 (.env)

项目根目录的 `.env` 配置示例：

```ini
# 代理配置（上游 kimik3.net 需要顺畅网络，推荐开启代理）
PROXY=http://127.0.0.1:10809

# 域名邮箱配置（方式二）
WORKER_DOMAIN=newmail.1840094972.workers.dev
EMAIL_DOMAIN=apollodorus.ccwu.cc
ADMIN_PASSWORD=mmc456123

# Outlook 邮箱文件（方式一，放置于项目根目录）
# outlook_live_20261018_222014.txt
# outlook_pointer.json

# 注册邮箱模式：auto（自动轮询）, outlook（仅Outlook）, domain（仅域名邮箱）
REGISTER_EMAIL_TYPE=auto

# 本地服务鉴权 Key（默认 kimik3）
PROXY_API_KEY=kimik3
```

---

## 🚀 启动与使用

### 1. 启动服务
运行 `启动脚本.bat` 或在虚拟环境中运行：
```bash
python main.py
```
服务将在 `http://127.0.0.1:8077` 启动，管理面板可直接访问 `http://127.0.0.1:8077/`。

### 2. 调用 API 示例 (OpenAI 兼容)

#### 请求模型列表
```bash
curl http://127.0.0.1:8077/v1/models \
  -H "Authorization: Bearer kimik3"
```

#### 对话请求 (DeepSeek V4 Flash)
```bash
curl http://127.0.0.1:8077/v1/chat/completions \
  -H "Authorization: Bearer kimik3" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "stream": false,
    "messages": [
      {"role": "user", "content": "你好，请介绍一下你自己"}
    ]
  }'
```

#### 流式对话
```bash
curl http://127.0.0.1:8077/v1/chat/completions \
  -H "Authorization: Bearer kimik3" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "stream": true,
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```
