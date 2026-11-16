# trace-dock server MVP

Python 3.12+ FastAPI 控制面 API，配置使用 Pydantic Settings，配套 CLI 位于 `../trace-dock`。只建 `participants` 和 `collections` 两张表；会话、排除项、大文件及 artifact 明细保存在 OSS manifest/JSONL 中。API 不接收 workspace 或轨迹文件上传。OSS 对象操作使用阿里云官方 OSS Python SDK v2，STS 使用官方 STS Python SDK；不保留手写签名实现。

## 安装与启动

```bash
uv venv --python 3.12
uv pip install -e .
cp .env.example .env
# 编辑 .env；应用启动时会自动读取，进程环境变量优先
.venv/bin/trace-dock-participants issue person@example.com
.venv/bin/trace-dock-server
```

服务启动函数位于 `trace_dock_server/http.py` 的 `main()`，上述命令由 `pyproject.toml` 的 `[project.scripts]` 生成。也可以在仓库根目录用 `.venv/bin/python main.py` 或 `.venv/bin/python -m trace_dock_server` 启动，三个入口共用相同的配置和启动逻辑。

## Docker / 阿里云 ECS 部署

仓库提供 `Dockerfile`、`compose.yaml`、`.env.production.example` 和 Nginx HTTPS 示例。Docker 默认执行 `python -m trace_dock_server`，容器监听 `0.0.0.0:8080`，Compose 默认只映射宿主机回环端口，生产数据库使用外部 PostgreSQL。

```bash
cp .env.production.example .env.production
chmod 600 .env.production
# 编辑 .env.production，填写 PostgreSQL、固定 pepper、OSS 和 RAM/STS 参数
docker compose config --quiet
docker compose build api
docker compose up -d api
curl -fsS http://127.0.0.1:8080/healthz
```

Compose 需为 2.30.0 或更高版本。完整的 ECS/RDS、证书和 Nginx、双节点 ALB、发放 token、日志及更新步骤见 [ECS Docker 部署技术方案](docs/DEPLOY-ECS.md)。

## 身份与数据持久化

CLI 安装在另一个仓库完成：下载对应平台的发布二进制，校验 `SHA256SUMS` 后放入 `PATH`；或在 CLI 仓库执行 `go build -trimpath -o "$HOME/.local/bin/trace-dock" ./cmd/trace-dock`。启动后用 `trace-dock --server https://<你的服务域名>` 进入采集流程。

`issue` 新建或重新签发高熵 token，仅当次向 stdout 显示；数据库只保存 `HMAC-SHA256(TOKEN_PEPPER, token)`。重新 issue 会撤销旧 token；`trace-dock-participants revoke person@example.com` 单独撤销。原始 token 应通过受控渠道交付本人，不写入配置或日志。pepper 必须在所有 API 节点保持一致，且持久保存在密钥管理系统中。

首次启动或发放身份时幂等初始化两张表。生产使用 PostgreSQL、数据库唯一约束、事务和行锁；SQLite 只供临时单进程开发使用。`collections` 比方案逻辑字段多几个内部协调字段：创建指纹、冻结规则 JSON、选择摘要、manifest 大小及已验证对象数，用于幂等和恢复；不新增其他数据表。

两张表均有 `created_at` 和 `updated_at`。数据库触发器负责维护 `updated_at`：新增时等于 `created_at`；参与者重新签发/撤销，以及任务状态、授权、统计或校验元数据实际变化时刷新为 UTC 时间。空更新、写入相同值、重复创建和重复完成等幂等操作不会改变 `updated_at`；事务回滚时更新时间一并回滚。PostgreSQL 使用 `TIMESTAMPTZ`，SQLite 使用带时区的 ISO 时间文本。

服务启动或发放身份时会在同一初始化事务中幂等补齐旧表字段和触发器。参与者历史行按 `revoked_at` 或 `created_at` 回填，任务历史行按 `completed_at`、`upload_confirmed_at`、`consented_at`、`created_at` 依次取已有值；未知的历史修改时间无法还原，回填值不代表完整历史审计。重复初始化不会重置已有 `updated_at`。PostgreSQL 的初始化通过事务级 advisory lock 串行化，支持多个 API 节点同时启动。

## 临时使用 SQLite

编辑服务端仓库的 `.env`：

```dotenv
TRACE_DOCK_DEV=1
DATABASE_URL=sqlite:////tmp/trace-dock-server.db
```

`TRACE_DOCK_DEV=1` 是 SQLite 的显式开关；数据库文件不存在时服务端会自动创建表。`pydantic-settings` 会自动读取服务端仓库根目录的 `.env`，并允许同名进程环境变量覆盖文件配置；也可以用 `TRACE_DOCK_ENV_FILE=/path/to/server.env` 指定其他配置文件。无需启动前手动导出变量。SQLite 只用于单进程临时验证，不用于两台 API 节点或生产数据。

参与者 token 仍通过服务端命令生成：

```bash
.venv/bin/trace-dock-participants issue person@example.com
.venv/bin/trace-dock-participants revoke person@example.com
```

使用真实配置验证 STS、OSS v2 写入、读取和删除权限：

```bash
.venv/bin/trace-dock-verify-oss
```

脚本只写入并删除一个随机 healthcheck 对象，路径遵循 `trajectory/token=<token>/dt=<yyyy-MM-dd>/job=healthcheck-<随机ID>/`；可用 `VERIFY_OSS_TOKEN` 指定测试路径中的 token，不会输出 AK、SK、STS 或完整 token。

API 契约详见 CLI 仓库的 `docs/API-MVP.md`：身份验证、创建任务、PATCH 状态、STS credentials、complete 五类接口，以及 `GET /healthz`。最终确认前拒绝签发 STS。服务端只下载小型 `manifest.json` 以获取对象列表，并通过 `HeadObject` 比较每个对象的大小和 OSS CRC64，不下载轨迹、workspace 或 JSONL 清单正文。同一任务重复完成不重复计数。可选设置 `OSS_READ_ENDPOINT` 为仅供服务端读取的 OSS endpoint；同地域 ECS 可使用 OSS 内网 endpoint，CLI 仍通过 `OSS_ENDPOINT` 获得公网直传地址。

生产部署使用两台无状态 API 节点，共享 PostgreSQL、OSS、TOKEN_PEPPER 和身份名单，放在终止 HTTPS 的负载均衡后，API 私网端口不直接对外开放。网关须关闭 Authorization/body 日志、限制 JSON 请求到 64 KiB、配置共享的认证限流（建议每 IP 每分钟 20 次）、设置 complete 的请求超时到 30 分钟，并根据 `/healthz` 摘除故障节点。进程内认证限流仅作单节点补充，不依赖转发头。默认监听 `127.0.0.1:8080`，同机反向代理可直接使用；独立负载均衡按私网部署设置 `HTTP_HOST` 和防火墙规则。

OSS 必须使用私有 bucket 和 HTTPS。服务端通过阿里云官方 OSS Python SDK v2 读取和校验对象，通过官方 STS Python SDK 获取临时凭证；代码不包含手写 OSS/STS 签名。对象键统一为 `trajectory/token=<token>/dt=<yyyy-MM-dd>/job=<job-id>/` 前缀，任务下的 manifest、轨迹、workspace 和清单都写在该前缀内。服务端账号需要读取当前前缀，以及对 `OSS_ROLE_ARN` 执行 `sts:AssumeRole`；session policy 只允许当前 Job 前缀的 PutObject、GetObject、ListParts、AbortMultipartUpload。STS 有效期 1 小时，CLI 到期重新申请，不落盘。不要给匿名用户 bucket 读写权限。region 默认 `cn-hangzhou`，endpoint、role 和 region 须与实际云环境一致。

`OSS_READ_ENDPOINT` 仅改变 API 节点读取对象元数据时使用的 endpoint，不改变返回给 CLI 的 `OSS_ENDPOINT`。API 节点部署在 bucket 同地域 ECS 时，可设置 OSS 内网 endpoint；不在该内网的开发机应留空。对象完整性校验采用 `Content-Length` 和 `x-oss-hash-crc64ecma`，服务端不重新下载对象正文。

规则在 `trace_dock_server/rules.json`，包含分类/密钥/大文件版本、白名单、黑名单和体积阈值。每次任务创建时冻结规则，之后修改默认规则不会改变已有 Job。发布新版本前须同步 CLI 适配器及技术方案，原始需求文档保留原文，不应直接提高某个旧版本的含义。

## 真实配置

真实部署时仍编辑服务端仓库的 `.env`，将临时 SQLite 配置改为 PostgreSQL，并填写稳定的 pepper、OSS bucket 和 RAM/STS 参数：

```dotenv
TRACE_DOCK_DEV=0
DATABASE_URL=postgresql://trace_dock:<password>@<postgres-host>:5432/trace_dock
TOKEN_PEPPER=<密钥管理系统提供的随机值，至少 32 字符>
HTTP_HOST=127.0.0.1
HTTP_PORT=8080
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET=<私有 bucket>
OSS_ROLE_ARN=acs:ram::<account-id>:role/<role-name>
ALIYUN_REGION=cn-hangzhou
ALIYUN_ACCESS_KEY_ID=<服务端 RAM 账号>
ALIYUN_ACCESS_KEY_SECRET=<服务端 RAM 密钥>
```

本机直接运行时，修改 `.env` 后重启服务端即可重新加载配置。Compose 部署读取 `.env.production`，修改后需 `docker compose up -d --force-recreate api` 重新创建容器。生产 API 放在 HTTPS 负载均衡之后；本机默认监听 `127.0.0.1:8080`，容器内监听 `0.0.0.0:8080`。`TOKEN_PEPPER` 在所有 API 节点必须一致，不能轮换成新值而不迁移既有 token。OSS bucket 应保持私有，STS role 只允许当前 `trajectory/token=<token>/dt=<yyyy-MM-dd>/job=<job-id>/` 前缀。

```bash
python3 -m unittest discover -s tests -v
```

生产 PostgreSQL、真实 OSS/STS、网关双节点故障切换和三平台完整交互需要在部署环境验收。
