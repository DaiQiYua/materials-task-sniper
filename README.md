# materials-task-sniper

materials.xlya.cn 任务自动狙击系统：Web 前后台，自动轮询可领取任务并秒级领取。

## 快速开始

```bash
python -m server          # 启动后浏览器打开 http://127.0.0.1:8787
python -m pytest test_webapp.py test_login.py -q   # 跑测试
```

旧版 CLI 脚本 `task_sniper.py` 仍可直接运行（`python task_sniper.py`），行为不变。

## 取 Token 的三种方式（仪表盘 Token 弹层）

| 方式 | 说明 |
|---|---|
| A. 账号密码 | 存多组手机号+密码，一键取 Token（直调登录 API，无验证码）。密码用 **Windows DPAPI 当前用户加密**落盘；**换 Windows 用户登录需重新录入** |
| B. 浏览器登录 | 打开原站登录页，人工登录后自动捕获（监听 Authorization 头 + localStorage 双路），约 3 分钟有效 |
| C. 手动粘贴 | 浏览器 F12 从请求头复制 JWT |

Token 过期时：引擎自动暂停 → 若已存凭据则**自动重新登录并恢复狙击**（全自动闭环）。

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
