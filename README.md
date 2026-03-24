# tg-nostr-bot

Telegram ↔ Nostr 消息桥。

将 Telegram 消息通过 NIP-17 Gift Wrap 协议转发到 Nostr 公开中继，实现 Telegram 与 Nostr 客户端之间的双向消息互通。

## 架构

```
[Telegram Bot] --webhook--> [CLI (FastAPI)]
                                    |
                              WebSocket
                                    |
                               [Gateway]
                                    |
                         Nostr Relay Pool (NIP-17)
                                    |
                            [Nostr Public Relays]
```

- **Gateway**：WebSocket 服务器 + Nostr 中继连接池。拥有所有私钥，负责 NIP-17 加密/解密。
- **CLI**：Telegram Webhook 接收器 + Gateway WS 客户端。纯数据透传，不做加密。
- **共享模块**：NIP-44 / NIP-17 加密、Nostr 中继连接池（来自 py_gateway）。

## 目录结构

```
tg-nostr-bot/
├── gateway/                  # Nostr 网关
│   ├── main.py               # 入口: python -m gateway.main --cwd-dir DIR
│   ├── config.py             # 配置加载
│   ├── websocket_server.py   # WS 服务器 + 消息路由 + NIP-17 加解密
│   ├── key_manager.py        # 来自 shared/
│   ├── relay_client.py       # 来自 shared/
│   ├── .env.example
│   └── requirements.txt
├── cli/                      # Telegram CLI 客户端
│   ├── main.py               # 入口: python -m cli.main --cwd-dir DIR
│   ├── config.py             # 配置加载
│   ├── app.py               # FastAPI Webhook + WS 客户端
│   ├── ws_client.py          # Gateway WebSocket 客户端
│   ├── .env.example
│   └── requirements.txt
└── shared/                   # 共享模块
    ├── key_manager.py        # NIP-44 / NIP-17 加解密
    └── relay_client.py       # Nostr 中继连接池
tests/
    ├── test_key_manager.py   # NIP-44 / NIP-17 / 密钥转换测试
    └── test_cross_compat.py  # 跨组件集成测试
```

> 密钥文件（`all_key.json`、`key.json`）由 `--cwd-dir` 指定，不在仓库目录内。

## 快速开始

### 1. 克隆并安装依赖

```bash
git clone https://github.com/nicholasyangyang/tg-nostr-bot.git
cd tg-nostr-bot

# 安装 Gateway 依赖
cd gateway && pip install -r requirements.txt && cd ..

# 安装 CLI 依赖
cd cli && pip install -r requirements.txt && cd ..
```

### 2. 配置 Gateway

```bash
cp gateway/.env.example gateway/.env
# 编辑 .env，配置 GATEWAY_HOST、GATEWAY_PORT、NOSTR_RELAYS
```

> **注意**：`--cwd-dir` 是启动命令参数，不是环境变量。

`.env.example` 默认值：

```env
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=7899
NOSTR_RELAYS=wss://relay.damus.io,wss://relay.0xchat.com,wss://nostr.oxtr.dev,wss://relay.primal.net
LOG_LEVEL=INFO
```

### 3. 配置 CLI

```bash
cp cli/.env.example cli/.env
# 编辑 .env，填入 BOT_TOKEN、WEBHOOK_URL、MSG_TO 等
```

> **注意**：`--cwd-dir` 是启动命令参数，不是环境变量。

`.env.example`：

```env
BOT_TOKEN=your_telegram_bot_token
WEBHOOK_URL=https://your-domain.com/bot
ALLOWED_USERS=123456789,987654321
PORT=8000
GATEWAY_WS_URL=ws://127.0.0.1:7899
MSG_TO=npub1...    # Telegram 消息默认发送到的 Nostr npub
LOG_LEVEL=INFO
```

> **MSG_TO**：Telegram 消息发送到 Nostr 的默认目标 npub。CLI 启动时如果没有本地 `key.json`，会自动向 Gateway 申请新的 npub 并保存。

### 4. 启动

```bash
# 终端 1：启动 Gateway（--cwd-dir 指定数据目录）
python -m gateway.main --cwd-dir ~/gateway-data/

# 终端 2：启动 CLI（--cwd-dir 指定数据目录）
python -m cli.main --cwd-dir ~/cli-data/
```

> `--cwd-dir` 为必填参数，用于存放密钥文件。不指定则报错退出。

### 5. Telegram Bot 设置

1. 在 Telegram 创建 Bot，获取 `BOT_TOKEN`
2. 配置公网可访问的 Webhook URL（`WEBHOOK_URL`）
3. 将你的 Telegram User ID 加入 `ALLOWED_USERS`
4. 向 Bot 发送消息，消息会被转发到 Nostr（发送到 `MSG_TO` 指定的目标 npub）

## 工作流程

### CLI 启动流程

1. 从 `--cwd-dir` 确定 `key.json` 路径（`{cwd-dir}/key.json`）
2. 读取本地 `key.json`
3. 若不存在，向 Gateway 发送 `{"type":"register_request"}`
4. Gateway 生成 npub/nsec，保存到 `all_key.json`，返回给 CLI
5. CLI 保存到本地 `key.json`
6. CLI 发送 `{"type":"register","npub":"..."}` 注册到 Gateway
7. Gateway 订阅该 npub 的 kind:1059 事件
8. 启动 FastAPI Webhook 服务

### 发送消息（Telegram → Nostr）

```
Telegram 消息 --> CLI /webhook --> WS --> Gateway (NIP-17 加密) --> Relay (kind:1059)
```

### 接收消息（Nostr → Telegram）

```
Relay (kind:1059) --> Gateway (NIP-17 解密) --> WS --> CLI --> Telegram Bot API
```

## WebSocket 协议

### CLI → Gateway

```json
// 注册请求（仅在 key.json 不存在时）
{"type": "register_request"}

// 注册 npub
{"type": "register", "npub": "npub1..."}

// 发送 DM
{"type": "dm", "from_npub": "npub1...", "to_npub": "npub1...", "content": "hello"}
```

### Gateway → CLI

```json
// 密钥响应
{"type": "register_done", "npub": "npub1...", "nsec": "nsec1..."}

// 收到 DM
{"type": "dm", "from_npub": "npub1...", "to_npub": "npub1...", "content": "hello"}
```

## 多实例支持

可以运行多个 CLI 实例，每个实例使用独立的 `--cwd-dir`（对应独立的 npub）：

```bash
python -m cli.main --cwd-dir ~/cli-data-1/
python -m cli.main --cwd-dir ~/cli-data-2/  # 不同端口、不同 --cwd-dir
```

- 每个实例向 Gateway 注册后，会自动订阅其 npub 的 DM
- Gateway 根据 `to_npub` 路由消息到对应 CLI 实例

## 安全说明

- **nsec 传输**：首次连接时 Gateway 通过 WebSocket 返回 nsec，仅限本地/可信网络使用。
- **Gateway 持有所有私钥**：CLI 不进行任何加密操作，所有 NIP-17 加解密由 Gateway 完成。
- **ALLOWED_USERS**：CLI 通过 Telegram User ID 白名单控制访问权限。
- `.gitignore` 已忽略 `*.env`、`*-data/`、`all_key.json`、`key.json`，确保密钥不泄露。
- **Gateway WS 通信**：Gateway 与 CLI 之间通过 WebSocket 通信，**必须部署在可信网络**（localhost 或私有 LAN）。`register` 接口不验证 npub 所有权，同一网络内的恶意进程可冒充任意 npub。
- **部署要求**：Gateway 和 CLI 应部署在同一台机器或私有网络中，不要暴露到公网。

## 可靠性

- **中继断线重连**：Relay 连接断开后自动重试，采用指数退避（10s → 最大 5min）。
- **CLI WebSocket 重连**：Gateway 连接断开后重试 3 次，每次退避 5s/10s/15s，3 次后 CLI 退出。
- **密钥持久化**：`all_key.json` 和 `key.json` 由 `--cwd-dir` 指定路径，使用原子写入（先写临时文件再 rename），确保密钥不损坏。

## 依赖

### Gateway

```
aiohttp
websockets
python-dotenv
secp256k1
bech32
cryptography
```

### CLI

```
aiohttp
websockets
fastapi
uvicorn
httpx
python-dotenv
pydantic
secp256k1
bech32
cryptography
```

## License

MIT
