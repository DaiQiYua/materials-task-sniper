# materials-task-sniper

materials.xlya.cn 任务自动狙击系统：Web 前后台，自动轮询可领取任务并秒级领取。

## 快速开始

```bash
python run.py --reload    # 开发模式（热重载），启动后浏览器打开 http://127.0.0.1:8787
python run.py             # 生产模式（无热重载）
python -m pytest tests -q # 跑全量测试
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

> ⚠️ **重启服务会中断正在运行的狙击**：`run.py` 释放端口时会杀掉旧进程，引擎随之停止，
> 任务池冻结（界面上的任务列表不再更新，标题行会显示「引擎未运行 · 最后轮询 …」）。
> 改后端代码需要重启时，先确认引擎状态，别在抢单中途重启。

旧版 CLI 脚本 `task_sniper.py` 仍可直接运行（`python task_sniper.py`），行为不变。
但它的**账号选择与 Web 版不同**（固定取 `claimEligibility.accounts[0]`，没有"排序靠前优先 /
多任务各领各的"的调度策略），功能完整、需要界面与多账号调度时请用 `python run.py`。

---

## 界面状态说明

界面上的状态分两层，**口径不同，别混用**：

- **子账号状态胶囊**：回答「这个平台子账号现在能不能领任务」。
- **任务速览 · 任务卡片状态**：回答「这条任务眼下是什么处境」。

### 平台子账号状态胶囊（7 种）

| 标签 | 含义 | 判定依据 |
|---|---|---|
| 领取中 | 有领取请求在途 | `status.account_inflight[账号] > 0` |
| 可领取 | 上游把这个号列进了任务的可领取名单，且当前可领 | `claimEligibility.accounts[].canClaim === true` |
| 待提交 | 该账号有任务领了还没提交 | 本机待提交记录（`(子账号, 任务)` 维度） |
| 冷却中 | 该账号 × 该任务处在任务冷却期（通常 3 天） | 本机任务冷却记录 / 上游 `nextClaimableAt` |
| 不可领 | 上游把该号列进了名单，但判定不能领（如"已有任务待首次提交"） | 上游 `disabledReason` / `reason` |
| 无可领 | 上游根本没给该号派任务（不在任何任务的可领取名单里） | 该账号不在任何 `claimEligibility.accounts` 中 |
| 无任务 | 当前任务池里没有该类型的任务 | 任务池为空 |

点击胶囊会 toast 出**完整依据**（上游 reason、账号级 `disabledReason`、冷却到期时刻、
待提交任务号等）；鼠标悬停也有同样的 tooltip。

### 任务速览 · 任务卡片状态（5 种）

| 标签 | 含义 | 判定依据（自上而下短路） |
|---|---|---|
| 待提交 | **本机已领取**该任务、还没提交 | 后端透出的 `localPendingAccounts` 非空 |
| 冷却中 | 本机冷却期未到（同一任务 `claimIntervalDays`，通常 3 天） | 后端透出的 `localCooldownUntil` |
| 已领完 | 配额领完 / 本账号领过 | `hasClaimed=true` 或 `claimedCount >= totalQuota`（旧写法只看 `remainingQuota<=0`，上游该值常年为 1，已弃用） |
| 可领取 | 至少有一个账号 `canClaim === true` | `claimEligibility.accounts[]` |
| 不可领 | 以上都不是 | tooltip 给出上游 `disabledReason` / `reason` 的人话翻译 |

**任务级 `canClaim` 只是参考**：它是上游按登录账号维度给出的聚合结论，同一条任务在不同
登录账号视图下并不一致；**真正决定"现在能不能领"的是账号级的 `accounts[].canClaim`**。
引擎的调度器同样以账号级为准。

**冷却信息只能来自本机**：上游任务的 `nextClaimableAt` 常年为空，冷却是本机按
`claimedAt + claimIntervalDays` 推出来的。所以后端在任务快照里按 `task_id` 额外透出
`localCooldownUntil` / `localCooldownAccounts` / `localPendingAccounts` 三个字段，
`/api/tasks` 与 SSE 的 `tasks` 事件都带它们。

### 文案约定

**所有状态标签统一 3 个字**（`可领取` / `冷却中` / `不可领` / `无可领` / `待提交` / `已领完` /
`无任务` / `领取中`）。完整语义一律放在 tooltip 里，不塞进标签。

### 任务速览的筛选

只有两个维度：**平台 tab** 和 **「只看可领取」**（按上面的"可领取"定义）。
速览**不按当前查看的子账号收窄**——那等于拿 `claimEligibility.accounts` 当可见性开关，
会把"平台已下发、但当前账号暂无资格领"的任务整体隐藏，看起来像"平台没下发任务"。
资格问题由状态标注表达。

### 刷新节奏

任务列表**内容不变时不重绘**（SSE 幂等），所以数字不动可能是"上游没新任务"，
也可能是"引擎没在跑"。标题行会同时给出**最后轮询时间**与引擎状态来区分：

```
共 67 · 3 可领 · 轮询于 18:21:03          ← 引擎运行中
共 67 · 3 可领 · 引擎未运行 · 最后轮询 18:19:47   ← 任务池已冻结
```

---

## 账号选择与调度策略

任务池里的每条任务要落到某个平台子账号头上，规则如下（`server/core.py::_schedule_once`）：

1. **同一时间只有一个任务下发 → 用「排序靠前」的账号领取。**
   排序依据：平台子账号列表（登录账号 → 子账号）的展示顺序 → 上游
   `claimEligibility.accounts` 里的顺序 → 账号 id。名次越小越优先。
   例：账号列表为 `[3057 桂花酒酿, 3056 春风不决, 2175 十年老粉]` 时，桂花酒酿最先抢。

2. **多个任务同时下发 → 每个账号分别领取不同任务。**
   同一轮调度里已经分配过的账号不再重复分配（配合 `per_account_inflight=1`，
   一个账号同一时刻只领一单）。账号不够时宁可让任务等下一轮，也不让一个账号连着抢多单。

3. **同一任务全局只分配给一个账号**（`_task_assigned`），因此不会出现两个账号抢同一条
   任务、其中一个白吃 409 —— 上游配额通常只有 1。

4. **重试沿用上次的账号**（`_task_last_account`）。换号再撞一次只会再浪费一个 409，
   而配额只有 1 的任务撞一次就没了。

5. **账号级资格为准**：`claimEligibility.accounts[].canClaim === true` 才算能领，
   任务级 `canClaim` 只作参考（见上一节）。

日志里会写明分配结果，便于核对为什么是某个号领的：

```
调度 #17715 [奶奶的牙好了，我们也更幸福] → 平台账号 3057（cred 1，账号排序靠前）
```

---

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

列表轮询遇到上游 429 会自动退避 5 秒；领取 429 / 网络异常会按退避持续重试（直到任务被抢走或从列表消失），409 直接放弃该任务。

---

## 轮询频率与上游限流口径

### 当前实际频率（实测）

| 项 | 值 | 来源 |
|---|---|---|
| 列表请求总频率 | **约 7.3 次/秒** | 实测采样（`stats.polls` 增量 / 采样时长） |
| 单任务类型令牌桶 | 5 次/秒 | `poll_rate_max/window = 5 / 1s`，按「登录账号 × 任务类型」分桶 |
| 每轮请求页数 | 2 页（尾页） | `tail_pages=2`、`page_size=10` → 20 条/轮 |
| 轮询周期 | 0.4 秒 | `poll_interval=0.4` |
| 校准页 | 每 10 秒 1 次 page1 | `total_refresh_seconds=10` |
| 轮询账号轮换 | 每 10 秒换一个登录账号 | `poll_rotate_seconds=10` |

即：`2 种任务类型 × 5 次/秒` ≈ 10 次/秒 的理论上限，实测 7.3 次/秒（差额是限速跳过与
上游响应耗时）。

### 上游限流：两个口径，别混

**① 上游声明值（领取侧）** —— 来自 `GET /api/user/tasks/claim-limits`：

```json
"requestLimits": { "requestLimit": 6, "windowSeconds": 10 }   // 6 次 / 10 秒
"windowLimit": 3, "cooldownSeconds": 10, "pendingCount": 3, "pendingLimit": 3
```

这组字段与 `reason=pending_limit`、`pendingCount/pendingLimit` 同属**领取侧**，
描述的是"这个登录账号还能不能领、能领几个"，**不是任务列表读接口的 QPS 配额**。

**② 实测值（任务列表读接口）** —— 单账号单 token，只读 `GET /api/user/tasks`：

| 压测方式 | 实际吞吐 | 状态码 | p50 | p95 |
|---|---|---|---|---|
| 固定 5/s | 5.0/s | 全 200 | 136ms | 175ms |
| 固定 10/s | 10.0/s | 全 200 | 155ms | 243ms |
| 固定 20/s | 20.0/s | 全 200 | 756ms | 955ms |
| 固定 40/s（目标） | 19.4/s（被打满） | 全 200 | 2173ms | 3902ms |
| 并发 4 / 8 / 16 | 16.8 / 15.8 / 17.1 /s | 全 200 | 226 / 506 / 852ms | 322 / 640 / 1218ms |
| 并发 32 | 6.3/s（排队崩塌） | 全 200 | 1509ms | 2207ms |
| 突发 60 并发 | 2.96s 全部完成 | **全 200，零 429** | — | 最大 2863ms |

结论：

- **列表读接口当前没有硬性限流**，实测吞吐天花板约 **16–17 次/秒**；
  再往上加不会提升吞吐，只会把延迟堆高（并发 32 时 p50 飙到 1.5s）。
- **性价比拐点在 10 次/秒**：10/s 时 p50 仅 155ms，20/s 时 p50 已 756ms（延迟 ×5）。
- 该结论只是"目前没拦"，不等于上游允许。上游随时可能把这套限制打开，
  届时会直接 429 并触发 `_block_polling(5.0)` 退避，反而错过新任务。

### 建议

任务下发本身是分钟级的，7.3 次/秒的收益极低、风险却集中在"被限流后错过新任务"。
若要收敛，推荐 `poll_interval=2.0` + `tail_pages=1`（约 1 次/秒），仍是官方声明口径的
2 倍、实测上限的 1/16，延迟稳定在 ~150ms。

---

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

### ⚠️ `pending_limit` 会挡住一切领取

上游 `claim-limits` 返回 `pendingCount=3, pendingLimit=3, reason=pending_limit` 时，
**这个登录账号的领取请求会被上游直接拒**（日志里表现为 `被限流 #xxxx，N 秒后重试`），
此时调度器再勤快也抢不到。排查顺序：

1. 看 `GET /api/status` 的 `pending_submissions` —— 列出是哪几条任务待提交；
2. 去平台提交/结算掉这些任务（提交后上游才会放开 `pendingCount`）；
3. 或点账号卡片里的「核对」，用领取记录刷新本机的待提交状态。

待提交数是**按登录账号**算的，不是按平台子账号；一个登录账号名下所有子账号的待提交会一起顶到这个上限。

---

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
server/core.py         狙击引擎（轮询/领取/限速/账号调度/自愈/401 自动暂停）
server/app.py          FastAPI REST + SSE + 同源中间件
server/login.py        登录取 token（API 直登 + Playwright 交互捕获 + 自动续登）
server/credentials.py  凭据存储（DPAPI 加密）
server/persist.py      token/config/history/凭据 落盘
web/app.js             仪表盘前端（无构建）：状态判定、任务速览、账号中心
web/app.css            仪表盘样式
task_sniper.py         旧版 CLI（保留）
tests/                 pytest 用例（跑全量：python -m pytest tests -q）
```

### 前端状态判定在哪儿

| 位置 | 职责 |
|---|---|
| `web/app.js::taskCardStatus()` | 任务速览里每张任务卡的状态（5 种） |
| `web/app.js::deriveTypeStatus()` / `accountTypeStatus()` | 平台子账号状态胶囊（7 种） |
| `web/app.js::STATUS_LABEL` | 状态标签文案表（统一 3 个字） |
| `server/core.py::_task_snapshot()` | 给任务快照透出本机冷却 / 待提交（`local*` 字段） |
| `server/core.py::_schedule_once()` | 账号选择与分配（排序靠前优先 / 多任务各领各的） |
