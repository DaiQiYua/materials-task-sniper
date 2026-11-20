# materials-task-sniper

materials.xlya.cn 任务自动狙击系统：Web 前后台，自动轮询可领取任务并秒级领取。

## 快速开始

```bash
python run.py --reload    # 开发模式（热重载），启动后浏览器打开 http://127.0.0.1:8787
python run.py             # 生产模式（无热重载）
python -m pytest test_webapp.py test_login.py -q   # 跑测试
```

**启动前自动释放端口**：若 8787 已被上次的实例占用，`run.py` 会识别并终止占用进程后再启动，
不再需要手动 `Stop-Process`（Windows 下端口被活跃监听时 uvicorn 报的是 `WinError 10013`，
看着像权限问题，实际只是重复启动）。

| 开关 | 说明 |
|---|---|
| `--no-free-port` | 关闭自动释放，端口冲突时直接报错 |
| `--force-free-port` | 占用进程"看起来不是本项目的服务"时也强制终止（默认只杀 python/uvicorn 且命令行含 `run.py`/`uvicorn`/`server.app` 的进程） |
| `--free-port-timeout N` | 终止后等待端口释放的秒数（默认 10） |

判断占用以 **netstat 监听表为主、真实 bind 探测为辅**：只看 bind 会漏判——占用方若设了
`SO_REUSEADDR` 并绑在 `0.0.0.0`（如 `python -m http.server`），本机再绑 `127.0.0.1` 会"成功"，
两个进程会静默共存。

旧版 CLI 脚本 `task_sniper.py` 仍可直接运行（`python task_sniper.py`），行为不变。

## 抢单性能参数（`PUT /api/config`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `poll_interval` | 0.4 | 每轮列表轮询间隔（秒）；默认由单个登录账号轮询（见 `single_poller`） |
| `tail_pages` | 2 | 每轮只取列表最后 N 页（新任务追加在尾部） |
| `total_refresh_seconds` | 10 | 回看 page=1 校准 total 的间隔 |
| `newest_first` | true | 按 `createdAt` 倒序优先抢最新任务 |
| `per_account_inflight` | 1 | 每个平台子账号同时最多几个领取请求 |
| `rate_limit_max/window` | 6 / 10s | **领取**请求令牌桶；按登录账号隔离 |
| `poll_rate_max/window` | 5 / 1s | **列表/配额**请求令牌桶；按「登录账号 × 任务类型」各自一个桶（不是只管登录账号：同一账号的不同任务类型各自限速，互不占额度） |
| `MAX_CLAIM_ATTEMPTS` / `MAX_PENDING_ATTEMPTS` | 4 / 6 | 引擎常量（不在 `PUT /api/config` 里）：同一任务连续限流（429）/「待处理已满」超过次数上限后**归零退避、不放弃任务**，把任务交回调度器重新评估（换账号或等冷却结束再抢），不会被永久标记为失败 |
| `claim_workers` | 3 | 领取线程数下限（1~20）；账号更多时自动补到账号数 |
| `bottom_first` | true | 兼容旧配置；`newest_first=true` 时不再使用 |
| `early_fire` | true | 按 `serverNowMs` 校时，冷却结束前 RTT/2 提前发射 |
| `single_poller` | true | **单账号轮询**：同一时刻只有一个登录账号刷任务列表（领取仍用全部账号） |
| `poll_rotate_seconds` | 10 | 单账号轮询时每隔多少秒换下一个登录账号；`0`=固定不换号 |

**单账号轮询（省请求/省限流额度）**：默认只有一个登录账号在刷列表，刷到的任务进池后
由**全部**平台子账号并行领取——「刷新只用一个账号，有任务才全账号去领」。
不同登录账号能看到的任务可能不同（各自绑定的平台子账号不同），所以默认每 10 秒轮换一次
刷新的账号，保证每个账号的任务迟早都能被发现；`poll_rotate_seconds=0` 只用一个账号（最省请求，
但看不到其他登录账号独有的任务），`single_poller=false` 回到「每个账号各刷一遍」的旧行为。

同一任务的平台子账号全局只会有一个在抢（防同平台自己挤自己）；失败后顺序换号重试。

列表轮询遇到上游 429 会自动退避 5 秒；领取 429 / 网络异常会按退避持续重试（直到任务被抢走或从列表消失），409 直接放弃该任务。


## 取 Token 的三种方式（仪表盘 Token 弹层）

| 方式 | 说明 |
|---|---|
| A. 账号密码 | 存多组手机号+密码，一键取 Token（直调登录 API，无验证码）。密码用 **Windows DPAPI 当前用户加密**落盘；**换 Windows 用户登录需重新录入** |
| B. 浏览器登录 | 打开原站登录页，人工登录后自动捕获（监听 Authorization 头 + localStorage 双路），约 3 分钟有效 |
| C. 手动粘贴 | 浏览器 F12 从请求头复制 JWT |

Token 过期时：引擎自动暂停 → 若已存凭据则**自动重新登录并恢复狙击**（全自动闭环）。

## 一键清空数据（恢复出厂）

顶栏右侧的垃圾桶图标按钮 → 弹层内输入「清空」两字才能执行，**不可恢复**。

清除内容：全部登录账号与密码（`credentials.json`）、所有 Token（`token.json` / `tokens.json`）、
平台子账号缓存（`platform_accounts.json`）、冷却/待提交状态（`cooldowns.json`）、
领取历史（`history.jsonl`）、日志及其轮转备份（`sniper.log*`），配置恢复出厂默认
（删除 `config.json`，回到内置默认值）。

执行前会自动停止狙击、关闭进行中的登录浏览器；完成后立即重建一个空的 `sniper.log`，
并通过 SSE 广播空快照让所有标签页同步。删除范围是白名单（仅上述数据文件），
不会递归删除目录、也不会碰 `.har` / `web/` / `server/` 等文件。

接口：`POST /api/data/clear`，body `{"confirm": "清空"}`；口令不符 → `400` 且不删除任何文件。

## 冷却检测（三类分开）

界面上的子账号状态按三类独立数据判断，**不用任务数量代替冷却**：

| 类型 | 时长 / 范围 | 数据来源 |
|---|---|---|
| 请求冷却 | 10 秒，登录账号级 | `claim-limits` 的 `nextAllowedAtMs` / `retryAfterSeconds` / `cooldownSeconds`；领取响应里的 `claimLimits` |
| 任务冷却 | 同任务 `claimIntervalDays`（通常 3 天） | 任务 `nextClaimableAt`；领取响应 + `/api/user/claims` 的 `claimedAt` |
| 待提交 | 领取后未提交 | 任务 `reason=pending_submission`；领取记录 `status=claimed` |

没有任务下发时：请求冷却仍可从 `claim-limits` 判断；任务冷却/待提交可通过
`POST /api/credentials/{cred_id}/sync-claims` 用领取记录补齐（启动时也会 best-effort 同步一次）。
核对接口：`GET /api/cooldowns`（request / tasks / pending / eligibility / claim_limits）。

**「待提交」语义已变化（与旧版不同，注意别按旧理解排障）**：

- 有领取后未提交的任务时，只阻塞**同一个任务**（同一平台子账号 × 同一任务），
  **不再**把整个账号冻结；其它任务照常抢。
- 账号级冻结只由上游 `claim-limits` 的 `pendingCount >= pendingLimit` 触发
  （「待处理已满」，属于上游规则，不是本地推断）。
- 每 **60s** 会用 `/api/user/claims` 自动同步一次领取记录来纠正待提交状态
  （已提交/已通过的领取会自动解掉待提交）；原来只有启动时同步一次 + 手动点「核对」，
  所以第三方提交后本地状态会长时间停留在「待提交」。

## 抓上游流量（调试/抓包）

本项目的上游请求走 `server/core.py` 里自写的 `HttpPool`（`http.client.HTTPSConnection` 长连接复用），
**不读 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量**，也不走系统代理，所以系统级代理对抢单流量完全无效
（这也是 Reqable 只能抓到浏览器 ↔ 本地 `8787`、抓不到真正上游请求的原因）。想看到上游请求，用下面两种方式：

### 方式一：Reqable TUN / 虚拟网卡模式（零改动，推荐先试）

1. Reqable 开启 **TUN 模式（虚拟网卡）**，实现全流量透明转发；
2. 把 Reqable 根证书安装到 Windows「**受信任的根证书颁发机构**」；
3. 本项目无需任何配置——`http.client` 默认 SSL 上下文读取 Windows 系统证书库，因此能正常解密。

### 方式二：只让狙击请求走代理（精确，重启生效或走 API 热更）

- 环境变量（启动前设置）：

  ```powershell
  $env:SNIPER_PROXY = "http://127.0.0.1:9090"
  python run.py
  ```

- 或调用 API（会写入 `config.json` 持久化）：

  ```powershell
  # 开启代理
  Invoke-RestMethod -Method Put http://127.0.0.1:8787/api/config `
    -ContentType 'application/json' -Body '{"proxy": "http://127.0.0.1:9090"}'

  # 关闭代理（恢复直连）
  Invoke-RestMethod -Method Put http://127.0.0.1:8787/api/config `
    -ContentType 'application/json' -Body '{"proxy": null}'
  ```

说明：

- 代理只影响 `/api/*` 引擎的上游请求（HTTPS 目标用 `CONNECT` 隧道，TLS 仍是端到端的；
  装好 Reqable 根证书后即可解密查看）。
- 代理值允许省略协议头（`127.0.0.1:9090` 会自动补成 `http://127.0.0.1:9090`），
  必须带端口；目前只支持 `http://host:port`，`https://` / `socks5://` 会被拒绝（API 返回 `422`）。
- 浏览器登录方式（Playwright）与 `urllib` 取 token 的行为不受影响（`urllib` 本身会读系统代理环境变量）。
- 换代理不需要重启连接池：代理已纳入连接 key，变更后自动新建连接，旧连接不会被复用。
- 默认（不设 `SNIPER_PROXY`、不设 `config.proxy`）行为与之前完全一致：直连。

## 安全说明

- 服务只绑 `127.0.0.1`，写接口校验 Host/Origin（防恶意网页跨站调用本地 API）
- token / 凭据 / 日志均不落 git（.gitignore）；token 与密码**永不写入日志**
- 非 Windows 平台凭据降级为明文存储（启动时 WARN），请注意文件权限
- `MATERIALS_DATA_DIR` 环境变量可改数据目录（默认项目根）

## 结构

```
server/core.py         狙击引擎（轮询/领取/限速/自愈/401 自动暂停）
server/app.py          FastAPI REST + SSE + 同源中间件
server/login.py        登录取 token（API 直登 + Playwright 交互捕获 + 自动续登）
server/credentials.py  凭据存储（DPAPI 加密）
server/persist.py      token/config/history/凭据 落盘
web/                   无构建仪表盘（SSE 实时推送）
task_sniper.py         旧版 CLI（保留）
```
