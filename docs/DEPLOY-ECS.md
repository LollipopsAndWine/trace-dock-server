# 阿里云 ECS Docker 部署

本方案将 FastAPI 服务作为无状态容器运行，PostgreSQL 保存身份和任务状态，OSS 保存轨迹、workspace 快照和清单。CLI 通过 HTTPS 调用 API，并凭临时 STS 直接上传 OSS。

## 1. 启动入口

三个入口调用同一个 `trace_dock_server.http.main()`，均会加载配置、初始化数据库并启动 Uvicorn：

```bash
# 仓库根目录直接启动
.venv/bin/python main.py

# Python 模块启动；Docker 默认使用此入口
.venv/bin/python -m trace_dock_server

# 安装包生成的命令入口
.venv/bin/trace-dock-server
```

先用 `uv pip install -e .` 安装依赖；本机启动默认读取仓库根目录 `.env`。Docker 通过运行时环境变量注入配置，不将 `.env`、token、AK/SK 或数据库密码复制进镜像。

## 2. ECS 与外部服务

单机部署链路：`CLI → HTTPS Nginx → ECS 127.0.0.1:8080 → API 容器`。API 连接 PostgreSQL 和 OSS；文件正文不经过 Nginx/API 转发。

在 ECS 安装 Docker Engine 和 Docker Compose 2.30.0 或更高版本，步骤见[阿里云安装文档](https://help.aliyun.com/en/ecs/user-guide/install-and-use-docker)。本项目使用 Compose 的 `env_file.format: raw`，避免密码中的 `$` 被当成插值，版本要求见[Docker Compose 文档](https://docs.docker.com/reference/compose-file/services/#format)。

准备可访问的 PostgreSQL 数据库 `trace_dock` 及账号。使用 RDS PostgreSQL 时，ECS 与 RDS 放在同一地域、同一 VPC，并将 ECS 私网 IP 加入 RDS 白名单；使用 RDS 内网地址连接。见[阿里云连接说明](https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/connect-to-an-apsaradb-rds-for-postgresql-instance-1)。首次启动需允许创建表、索引、触发器和触发器函数；后续启动会幂等补齐字段。

使用私有 OSS bucket，配置与实际地域一致的 endpoint、region、RAM 凭据和 STS role。当前代码使用环境变量中的 RAM AK/SK，需要读取上传对象以及签发限定当前 Job 前缀的 STS；`trace-dock-verify-oss` 另需对随机 healthcheck 对象的写入、读取和删除权限。API 节点在 bucket 同地域 ECS 时，可设置 `OSS_READ_ENDPOINT` 为该地域 OSS 内网 endpoint；该配置只供服务端通过 `HeadObject` 读取对象大小和 CRC64，返回给 CLI 的 `OSS_ENDPOINT` 保持公网地址。开发机或其他不能访问该 VPC 内网的环境不要设置它。

## 3. 构建并启动

将仓库放在 ECS，例如 `/opt/trace-dock-server`，在该目录执行：

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

编辑 `.env.production`，替换所有 `CHANGE_ME` 占位值。使用不含引号的 `KEY=value`，Compose 按原值读取。数据库 URL 的密码若含 `@`、`#`、`:`、`/` 等特殊字符，需要 URL 编码。`TOKEN_PEPPER` 使用独立生成、持久保存的随机值，至少 32 字符；可以用 `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` 生成一次。

```bash
docker compose version
docker compose config --quiet
docker compose build api
docker compose up -d api
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
```

启动后健康检查应返回 `{"status":"ok"}`。健康检查表示 API 进程能够响应；它不验证真实 OSS/STS 权限。进一步发放测试身份并运行一次小范围采集，可验证数据库、STS、OSS 上传和完成校验的完整链路。

容器内监听 `0.0.0.0:8080`，Compose 默认只映射到宿主机 `127.0.0.1:8080`，供同机 Nginx 访问。PostgreSQL 地址不能填写宿主机的 `127.0.0.1`，因为容器有独立网络；应填 RDS 私网域名或可达的数据库服务地址。

镜像以 UID/GID `10001:10001` 运行，生产 Compose 使用只读根文件系统及临时 `/tmp`，不把 SQLite 当作生产数据库，也不在容器内持久化轨迹文件。容器重建后，身份与任务仍由外部 PostgreSQL 保存。

## 4. 发放身份与验证 OSS

```bash
# 仅当次在终端显示新 token；重新 issue 同一邮箱会撤销旧 token
docker compose run --rm --no-deps api trace-dock-participants issue person@example.com

# 单独撤销
docker compose run --rm --no-deps api trace-dock-participants revoke person@example.com

# 真实云权限验证：写入并删除一个随机 healthcheck 对象
docker compose run --rm --no-deps api trace-dock-verify-oss
```

token 通过受控渠道交付本人，不写进 Dockerfile 或镜像。不要把 `docker compose config` 的完整输出保存或转发，其中可能包含运行时凭据；校验配置使用前述 `--quiet`。

## 5. HTTPS 域名与 Nginx

域名解析到 ECS，准备该域名的证书。按服务器发行版安装 Nginx，将 `deploy/nginx.conf.example` 复制到 Nginx 的 `http` 配置目录，例如 `/etc/nginx/conf.d/trace-dock.conf`，替换 `collector.example.com` 以及证书文件路径，再执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -fsS https://collector.example.com/healthz
```

配置将请求转发到宿主机回环地址，API 端口不对公网开放；ECS 安全组对参与者开放 443，80 仅用于跳转 HTTPS。示例关闭访问日志、限制控制请求正文为 64 KiB，并将上游读取超时设为 1900 秒，以覆盖客户端最长 30 分钟的同步对象校验请求。

示例 Nginx 会重新设置 `X-Forwarded-For` 为实际连接 IP，覆盖外部传入值。`.env.production.example` 中的 `FORWARDED_ALLOW_IPS=*` 只适用于默认回环端口绑定和可信反向代理；这样容器中的 Uvicorn 可以取得参与者 IP，避免所有用户共享代理 IP 的认证限流桶。若把 API 暴露给其他来源，应改为实际可信代理 IP/CIDR，并限制访问来源。

参与者使用正式 HTTPS 地址：

```bash
trace-dock --server https://collector.example.com
```

## 6. 两台 ECS 与 ALB

两台 ECS 部署同一个镜像，连接同一 PostgreSQL、OSS bucket，并使用同一 `TOKEN_PEPPER` 和身份名单。每台 ECS 在 Compose 目录的 `.env` 中保存本机私网绑定地址，例如：

```dotenv
TRACE_DOCK_BIND_IP=10.0.1.10
```

这里的 `.env` 用于 Compose 变量替换，服务凭据仍保存在 `.env.production`。保存后执行：

```bash
docker compose up -d api
```

ALB 使用 HTTPS 监听和域名证书，后端连接 ECS 私网 `8080`，健康检查路径为 `/healthz`。安全组只允许 ALB 访问该端口；根据 ALB 来源设置可信转发代理范围。若端口改为私网绑定，同机 Nginx 示例不再适用，应直接由 ALB 转发或相应调整 Nginx 上游地址。

当前 `/complete` 同步读取 OSS 对象并计算哈希，请求可能持续 30 分钟。接入 ALB 前，必须确认监听的请求和空闲超时都能覆盖这一时长；阿里云目前只有升级后的 ALB 实例能在申请相应配额后配置到 3600 秒，未升级实例的实际上限为 900 秒。不能直接沿用默认超时。见[ALB 配额与限制](https://help.aliyun.com/zh/slb/application-load-balancer/quotas-and-limits)。

## 7. 更新与停止

```bash
# 检查状态及日志
docker compose ps
docker compose logs --tail=100 api

# 更新代码或 rules.json 后重新构建镜像
docker compose build api
docker compose up -d api

# 修改 .env.production 后重新创建容器，单纯 restart 不会重新注入环境变量
docker compose up -d --force-recreate api

# 停止，保留 PostgreSQL 和 OSS 数据
docker compose stop api
```

双节点更新时，先从负载均衡摘除一个节点，等其正在执行的校验完成，再更新该节点并重新挂回；另一节点按相同步骤处理。Compose 保留 35 分钟退出宽限，避免用 Docker 默认短超时中断长校验。不要在更新中更换 `TOKEN_PEPPER`；大文件/扫描规则变化只作用于新建任务，已有任务保持冻结规则。

当前参与者 token 仍出现在 OSS 对象路径、manifest 前缀及数据库 manifest_key 中。容器化不会改变这一已知行为，正式生产上线前需要完成凭据路径调整。真实 RDS/OSS、HTTPS 证书、ALB 超时和双节点切换须在目标阿里云环境验证。
