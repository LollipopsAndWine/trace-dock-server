# Trace Dock Server

Trace Dock Server 是 Trace Dock CLI 的控制面服务。它负责身份验证、创建和恢复任务、发放限定当前任务前缀的临时 OSS 凭证，以及在上传完成后校验 manifest 和对象 CRC64。

文件正文不会经过 API 服务。CLI 直接把已授权的轨迹和 workspace 内容上传到私有 OSS，服务端只保存参与者和任务状态，并读取小型 manifest 完成校验。

## 运行前准备

本地开发需要：

- Python 3.12+
- `uv`
- SQLite（仅限单进程临时验证）

生产部署还需要：

- PostgreSQL 数据库
- 私有 OSS bucket
- 可以签发限定 Job 前缀权限的 RAM/STS role
- HTTPS 域名和反向代理或负载均衡
- Docker Engine 与 Docker Compose 2.30+

CLI 位于相邻的 [`trace-dock`](../trace-dock) 仓库。服务端启动后，参与者使用 CLI 连接服务端；不要把 token 写入 CLI 配置、镜像或日志。

## 本地启动

### 1. 安装依赖

在服务端仓库根目录执行：

```bash
uv venv --python 3.12
uv pip install -e .
cp .env.example .env
chmod 600 .env
```

服务端会自动读取仓库根目录的 `.env`，进程环境变量优先。也可以通过 `TRACE_DOCK_ENV_FILE` 指定其他配置文件。

### 2. 配置 SQLite

编辑 `.env`，本地只需要先启用 SQLite，并填入一份仅供开发的 pepper 和 OSS 占位配置：

```dotenv
TRACE_DOCK_DEV=1
DATABASE_URL=sqlite:////tmp/trace-dock-server.db
TOKEN_PEPPER=dev-only-random-value-with-at-least-32-characters
HTTP_HOST=127.0.0.1
HTTP_PORT=8080
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET=local-placeholder
OSS_ROLE_ARN=acs:ram::local:role/placeholder
ALIYUN_REGION=cn-hangzhou
ALIYUN_ACCESS_KEY_ID=local-placeholder
ALIYUN_ACCESS_KEY_SECRET=local-placeholder
```

SQLite 只适合单进程验证身份和 API 流程。要实际上传和校验对象，仍需替换为有效的 OSS bucket、RAM 凭据和 STS role。

### 3. 启动服务

以下入口等价：

```bash
.venv/bin/trace-dock-server
# 或
.venv/bin/python -m trace_dock_server
# 或
.venv/bin/python main.py
```

确认服务可用：

```bash
curl -fsS http://127.0.0.1:8080/healthz
```

预期返回：

```json
{"status":"ok"}
```

### 数据库结构变更

表结构统一定义在 `trace_dock_server/models.py`。服务启动时会运行 Alembic 升级；新数据库由模型创建完整表结构，已有数据库按迁移版本补齐。应用读写使用 SQLAlchemy ORM。

新增字段或表时，先更新模型，再生成并检查迁移：

```bash
.venv/bin/alembic revision --autogenerate -m "add user profile field"
.venv/bin/alembic upgrade head
```

提交模型和生成的迁移文件。部署启动时会自动执行 `upgrade head`，不需要再在数据库启动代码里为字段增加单独分支。

### 4. 创建参与者 token

数据关系是：`users.email` 唯一；`participants` 每行对应一个 token 和一个 participant ID，并通过 email 外键关联 user。因此一个邮箱可以同时有多个活动 token。每次 `issue` 创建新的 token / participant ID，只在标准输出显示新 token 一次，不影响同邮箱之前签发的 token。`revoke` 会撤销该邮箱的所有活动 token，历史记录仍保留。

```bash
.venv/bin/trace-dock-participants issue person@example.com
```

安全地把输出中的 token 和 participant ID 交给本人。撤销该邮箱下所有活动 token：

```bash
.venv/bin/trace-dock-participants revoke person@example.com
```

新签发的 token 加密携带邮箱。数据库不可用时，可用保存的 `TOKEN_PEPPER` 从 token 恢复邮箱，不需要连接数据库：

```bash
.venv/bin/trace-dock-token-email
```

命令会隐藏式提示输入 token，只输出邮箱。旧格式 token 不包含邮箱，不能恢复。

### 5. 连接 CLI

CLI 当前通过 npm 全局安装：

```bash
npm install -g @pjlab/trace-dock
trace-dock --allow-http --server http://127.0.0.1:8080
```

`--allow-http` 只用于本机测试。生产服务必须通过 HTTPS 访问。

## 生产部署

生产环境使用 PostgreSQL 和外部 OSS，不使用 SQLite。推荐链路为：

```text
Trace Dock CLI → HTTPS Nginx / ALB → API 容器 → PostgreSQL
                                      ↘ 私有 OSS
```

API 节点是无状态的；多个节点必须共享同一个 PostgreSQL、OSS bucket、参与者名单和 `TOKEN_PEPPER`。

### 1. 准备生产配置

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

编辑 `.env.production`，替换所有 `CHANGE_ME`。不要给值加引号，Compose 会按原值读取。数据库密码中的 `@`、`#`、`:`、`/` 等字符需要先进行 URL 编码。

至少需要配置：

```dotenv
TRACE_DOCK_DEV=0
DATABASE_URL=postgresql://trace_dock:<password>@<postgres-host>:5432/trace_dock?sslmode=require
TOKEN_PEPPER=<稳定且随机的值，至少 32 字符>
HTTP_HOST=0.0.0.0
HTTP_PORT=8080
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET=<私有 bucket>
OSS_ROLE_ARN=acs:ram::<account-id>:role/<role-name>
ALIYUN_REGION=cn-hangzhou
ALIYUN_ACCESS_KEY_ID=<服务端 RAM 账号>
ALIYUN_ACCESS_KEY_SECRET=<服务端 RAM 密钥>
```

生成 pepper 的示例：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

`TOKEN_PEPPER` 必须在所有 API 节点保持一致并妥善备份。它用于验证 token，也用于解密新 token 中的邮箱；丢失后既有 token 无法验证，也无法从新 token 恢复邮箱。不要在更新过程中随意更换。

### 2. 构建并启动容器

```bash
docker compose version
docker compose config --quiet
docker compose build api
docker compose up -d api
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
```

Compose 默认只把容器端口绑定到宿主机 `127.0.0.1:8080`，适合由同机 Nginx 转发。PostgreSQL 地址不能填容器内的 `127.0.0.1`，应填写 RDS 私网地址或容器可达的数据库地址。

镜像以非 root 用户运行，使用只读根文件系统，容器重建不会丢失 PostgreSQL 中的身份和任务数据。不要把 `.env.production`、token、AK/SK 或数据库密码复制进镜像。

### 3. 配置 HTTPS

不要把 API 的 HTTP 端口直接暴露给公网。将 `deploy/nginx.conf.example` 配置到 Nginx，替换域名和证书路径，然后检查并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -fsS https://collector.example.com/healthz
```

生产反向代理应关闭 `Authorization` 和请求正文日志，并将长时间的 `/complete` 校验请求超时设置为至少 30 分钟。完整的 ECS、RDS、OSS、Nginx、ALB 和双节点更新步骤见 [ECS Docker 部署说明](docs/DEPLOY-ECS.md)。

### 4. 验证云权限

发放测试身份并验证 OSS 的写入、读取和删除权限：

```bash
docker compose run --rm --no-deps api trace-dock-participants issue person@example.com
docker compose run --rm --no-deps api trace-dock-verify-oss
```

`trace-dock-verify-oss` 只创建并删除一个随机 healthcheck 对象，使用独立的随机 healthcheck participant ID，不使用参与者 Bearer token。

## 配置参考

| 变量 | 用途 |
| --- | --- |
| `TRACE_DOCK_DEV` | 设为 `1` 才允许 SQLite；生产设为 `0` |
| `TRACE_DOCK_ENV_FILE` | 指定配置文件路径 |
| `DATABASE_URL` | PostgreSQL 或开发用 SQLite 连接串 |
| `TOKEN_PEPPER` | token 校验和邮箱恢复密钥，至少 32 字符，所有节点必须一致并备份 |
| `HTTP_HOST` / `HTTP_PORT` | API 监听地址和端口；容器通常为 `0.0.0.0:8080` |
| `OSS_ENDPOINT` | 返回给 CLI 直传使用的 HTTPS endpoint |
| `OSS_READ_ENDPOINT` | 服务端读取 OSS 元数据的可选 endpoint；同地域 ECS 可使用内网地址 |
| `OSS_BUCKET` | 私有 OSS bucket |
| `OSS_ROLE_ARN` | 用于签发当前 Job 前缀临时凭证的 RAM role |
| `ALIYUN_REGION` | OSS / STS 地域，默认 `cn-hangzhou` |
| `ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET` | 服务端 RAM 凭据 |

API 节点不应设置无法访问的 `OSS_READ_ENDPOINT`。该变量只影响服务端读取对象大小和 CRC64，不改变返回给 CLI 的 `OSS_ENDPOINT`。

## API 与日常操作

### 健康检查

```bash
curl -fsS https://collector.example.com/healthz
```

开发或内网环境可以直接打开自动生成的接口文档：

- Swagger UI：`https://collector.example.com/docs`
- ReDoc：`https://collector.example.com/redoc`
- OpenAPI JSON：`https://collector.example.com/openapi.json`

文档会显示请求体字段、类型、必填项、枚举和 Bearer 认证方式。生产环境请只在受保护的内网或管理入口开放这些页面。

### 主要接口

CLI 使用 Bearer token 调用以下控制面接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查，不需要 token |
| `POST` | `/v1/identity/verify` | 验证参与者身份 |
| `POST` | `/v1/collections` | 创建或幂等恢复任务 |
| `PATCH` | `/v1/collections/{job_id}` | 更新选择、授权和上传状态 |
| `POST` | `/v1/collections/{job_id}/credentials` | 获取当前 Job 的临时 OSS 凭证 |
| `POST` | `/v1/collections/{job_id}/complete` | 校验 manifest 和上传对象并完成任务 |

API 不接收轨迹或 workspace 文件上传，控制请求正文上限为 64 KiB，也不接受带 query string 的请求。完整 JSON 字段和状态流转见 CLI 仓库的 [API-MVP.md](../trace-dock/docs/API-MVP.md)。

其中 `/v1/identity/verify` 和 `/v1/collections/{job_id}/credentials` 只需要 Bearer token 和路径参数，不需要请求体。新任务的 OSS 路径格式为 `trajectory/participant_id=<participant_id>/dt=<date>/job=<job_id>/`，不含邮箱或 Bearer token；manifest schema 1.1 任务需重新创建。

### 日志、更新与回滚

```bash
docker compose logs --tail=100 api
docker compose build api
docker compose up -d --force-recreate api
```

更新多节点时，先从负载均衡摘除一个节点，等待正在执行的校验完成，再更新并恢复流量；另一节点按相同步骤处理。不要在更新期间更换 `TOKEN_PEPPER`。

## 安全注意事项

- 生产 API 只通过 HTTPS 暴露，API 容器端口保持私网或回环绑定。
- OSS bucket 必须是私有 bucket；STS policy 只允许当前 Job 前缀的必要操作。
- 不要记录或转发 `Authorization`、token、`TOKEN_PEPPER`、AK/SK、数据库密码、`.env.production` 或 `docker compose config` 的完整输出。
- users 表保证邮箱唯一；participants 的每行通过 email 外键关联一个 user，并各自对应一个 token 和 participant ID。token 在数据库中保存为 pepper HMAC；新 token 加密携带邮箱，只有持有 `TOKEN_PEPPER` 才能恢复，原文只在 `issue` 命令输出一次。
- 同一邮箱每次签发会新建 participant ID，已有 token 保持有效；`revoke <email>` 会撤销该邮箱所有活动 token。新任务使用 `trajectory/participant_id=<participant_id>/...` OSS 前缀，该 ID 不是认证凭据。已存在的旧对象不会自动搬迁或删除；若旧 key 曾被公开，需轮换相关 token 并按存储保留策略清理旧对象。
- 规则在任务创建时冻结。更新 `trace_dock_server/rules.json` 后重启服务端，新规则只对新建任务生效。

## 常见问题

### 启动时报配置错误

检查 `.env` 是否包含全部必填项，`TOKEN_PEPPER` 是否至少 32 字符，`DATABASE_URL` 是否为 PostgreSQL；SQLite 只有在 `TRACE_DOCK_DEV=1` 时才允许使用。

### `/healthz` 正常但上传失败

健康检查只证明 API 进程能响应，不验证 OSS 或 STS。运行 `trace-dock-verify-oss`，检查 bucket、endpoint、region、RAM 凭据和 `OSS_ROLE_ARN` 的权限。

### token 无法验证

确认 CLI 使用的是仍有效的 `issue` token。一个邮箱签发的多个 token 可分别使用；`revoke <email>` 会全部撤销。多节点部署时确认所有节点使用同一个 `TOKEN_PEPPER` 和数据库。

### Docker 启动后连接不到数据库

容器中的 `127.0.0.1` 指向容器本身。将 `DATABASE_URL` 改为 RDS 私网域名、Docker 网络中的数据库服务名或其他容器可达地址。

## 开发与测试

运行单元测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

发布前还应在目标云环境验证真实 PostgreSQL、OSS/STS、HTTPS 证书、长时间 complete 校验和多节点切换。部署细节见 [ECS Docker 部署说明](docs/DEPLOY-ECS.md)。
