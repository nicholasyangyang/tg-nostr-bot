# Key Management --cwd-dir Design

## 概述

CLI 和 Gateway 的 key 文件路径改为统一由命令行 `--cwd-dir` 参数指定，彻底消除相对路径带来的歧义和 bug。

## 变更

### 1. CLI (`cli/`)

**`cli/main.py`**
- 新增 `--cwd-dir` 参数（必填）：
  ```
  python -m cli.main --cwd-dir /path/to/data
  ```
- 不传 `--cwd-dir` → 打印 usage 并以 exit code 1 退出
- `key_path = {cwd_dir}/key.json`
- 启动时：
  - `key.json` 存在 → 加载已有 npub
  - `key.json` 不存在 → 发 `register_request` 到 Gateway → 保存到 `key.json`
- 用该 npub 注册到 Gateway

**`cli/config.py`**
- `KEY_PATH = os.getenv("KEY_PATH", "key.json")`（仅文件名，无目录）

**`cli/ws_client.py`**
- `connect_and_register()` 中：`KEY_PATH` 现在是相对路径，由调用方拼完整路径传入
- 修改调用处传入完整路径

**`cli/.env.example`**
- 新增 `CWD_DIR=` 行（注释说明必填）

### 2. Gateway (`gateway/`)

**`gateway/main.py`**
- 新增 `--cwd-dir` 参数（必填）：
  ```
  python -m gateway.main --cwd-dir /path/to/data
  ```
- 不传 `--cwd-dir` → 打印 usage 并以 exit code 1 退出
- `all_key_path = {cwd_dir}/all_key.json`
- 启动时：
  - 文件存在 → 加载到 `_all_keys`，填充 `_npub_to_seckey`
  - 文件不存在 → 创建空 `{}`，立即原子写入文件

**`gateway/config.py`**
- `ALL_KEY_PATH = os.getenv("ALL_KEY_PATH", "all_key.json")`（仅文件名，无目录）
- 删除 `Path(__file__).resolve().parent` 的默认值逻辑（因为 `--cwd-dir` 已保证绝对路径）

**`gateway/.env.example`**
- 新增 `CWD_DIR=` 行（注释说明必填）

### 3. shared/key_manager.py

- 不改动，`get_keys()` 和 `generate_keys()` 由调用方传绝对路径

## 文件路径汇总

| 组件 | key 文件 | 路径 |
|------|---------|------|
| CLI | `key.json` | `{cwd_dir}/key.json` |
| Gateway | `all_key.json` | `{cwd_dir}/all_key.json` |

## 使用示例

```bash
# 启动 gateway（数据存 ~/gateway-data/）
python -m gateway.main --cwd-dir ~/gateway-data/

# 启动 cli（数据存 ~/cli-data/）
python -m cli.main --cwd-dir ~/cli-data/
```

## 错误处理

- `--cwd-dir` 目录不存在 → 创建（包括父目录）
- key 文件无法读写 → 报错退出
- 不传 `--cwd-dir` → usage 提示并退出

## 测试计划

1. CLI 无 `--cwd-dir` → 应报错退出
2. Gateway 无 `--cwd-dir` → 应报错退出
3. CLI + Gateway 使用不同 `--cwd-dir` → 各自正常读写 key 文件
4. CLI 已有 `key.json` → 使用已有 npub
5. CLI 无 `key.json` → 请求 Gateway 生成
6. Gateway 无 `all_key.json` → 创建空文件后正常启动
