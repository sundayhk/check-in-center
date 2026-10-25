# signin-hub

多站点自动签到系统：NodeSeek（含随机签到 + Cloudflare）、V2EX、烧饼社区（linux.sb）、烧饼论坛（sb.sb）。
插件式架构，新增站点只需在 `sites/` 加一个类并登记进 `REGISTRY`。

## 架构

```
main.py            入口：参数解析、装配、汇总、通知
core/config.py     配置加载（YAML + 环境变量插值，缺文件时纯环境变量模式）
core/http.py       L1 请求层：curl_cffi 指纹伪装，requests 兜底
core/browser.py    L2/L3 浏览器层：Camoufox 优先，Playwright 兜底，含 Turnstile 点击
core/base.py       BaseSite 抽象类 + SigninResult 统一返回 + Context 依赖注入
core/state.py      SQLite：当日去重、历史留痕
core/notify.py     Telegram / 企业微信 / 通用 Webhook / 标准输出
core/runner.py     串行调度、随机延迟、失败重试、汇总渲染
sites/             各站点适配器
```

请求层三级降级（只在被拦时才加重手段）：

| 层级 | 手段 | 触发条件 |
|---|---|---|
| L1 | curl_cffi 伪装真实 Chrome 的 TLS/JA3 指纹 | 默认 |
| L2 | Camoufox / Playwright 真实浏览器指纹 | L1 返回 403/503/429 或 `cf-mitigated: challenge` |
| L3 | 自动点击 Turnstile 复选框并等待 `cf_clearance` | L2 仍停在挑战页 |

## 快速开始

```bash
cd signin-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 浏览器通道（NodeSeek 必装，其他站可选）
python -m camoufox fetch          # 推荐
# 或 python -m playwright install chromium

cp config.example.yaml config.yaml
cp .env.example .env              # 填入 cookie / TG 凭证

# .env 会被程序自动加载（本地直跑、Docker、cron 都生效），不需要手动 export
python main.py --doctor           # 体检：依赖 / 浏览器内核 / 配置 / 通知
python main.py --list-sites
python main.py --test-notify      # 验证 TG 能否收到
python main.py --dry-run          # 演练，不发起请求
python main.py                   # 真跑
```

## 各站点说明

### NodeSeek

- 没有稳定的公开签到接口，社区方案普遍是浏览器点按钮，因此**默认走浏览器通道**
  （`strategy: auto` 会先试接口，失败自动升级；`browser` 则直接用浏览器，最稳）
- 流程：注入 cookie → 打开 `/board` → 过 CF → 点 `.board-intro` 内的按钮 → 正则解析鸡腿数
- 签到模式 `mode`：`random`（试试手气，1~10 鸡腿）或 `fixed`（稳拿 5 鸡腿），可按账号覆盖
- **Cookie 必须整段复制**：F12 → Network → 刷新 → 点任一 nodeseek 请求 → Request Headers → 复制整条 Cookie。
  用控制台 `document.cookie` 拿不到 `session`（HttpOnly），会一直显示未登录
- 多账号 cookie 用 `|` 分隔

### V2EX

- `GET /mission/daily` 页面里找 `/mission/daily/redeem?once=NNNN`，再 GET 一次即领取
- Cookie 只要 `A2` 那一段
- 北京时间 08:00 前一般领不了，建议排在 08:30 之后
- 基本无 Cloudflare；但 2026-10 起部分出口 IP（数据中心/大陆）会被 403，需配置 PROXY 代理；
  curl_cffi 请求即可通过（无需浏览器）

### 烧饼论坛 sb.sb（站点 key：`sbsb`）

- 2026-10 改版：签到页迁到 `https://sb.sb/checkin/`（旧 `/signin/` POST 会 404），
  且提交前必须过 cap.js 人机验证（PoW，站点私有 wasm，脚本无法离线求解）
- 默认走浏览器通道：点「立即签到」→ 弹出 cap 模态框 → 点击 widget 自动完成 PoW → token 写入表单自动提交
- 该站对部分地区封锁（直连所有路径返回伪装 404 占位页），需配置 `runtime.proxy`（或 .env 的 PROXY）走可访问出口
- 「已签到」判定：按钮文案含「今日已签到」，或页面存在 `.signin-cal-day.done.today`
- 奖励随机 1~10 烧饼，按 UTC 日期计，北京时间每天 08:00 刷新，建议排在 08:30 之后
- Cookie 整段复制（登录后抓任一 sb.sb 请求的 Request Headers）
- Cookie 失效时若配了 `username/password`，浏览器通道会自动登录后再签（尽力而为）

### 烧饼社区 linux.sb（站点 key：`linuxsb`）

- `GET /daily_checkin` 取 `_csrf` → `POST /daily_checkin`（`_csrf=xxx`）
- 登录态 cookie 是 `bbs_auth`，CSRF 取页面隐藏域或 cookie `bbs_csrf`
- 2026-08 起**间歇性**开 Cloudflare 托管挑战：L1 被拦会自动升级到浏览器通道
- Cookie 失效时，若配置了 `username/password`，会自动登录（页面算术验证码由脚本解、PoW 由页面 JS 算）
  ；若触发邮箱验证风控，只能人工在本地浏览器过一次再更新 Cookie
- 多账号 cookie 用 `&` 分隔

## Web 控制台

容器内已内置 Web UI（`python main.py --web`，与 `--daemon` 可同时运行）：

```bash
# .env 里设置访问密码（公网部署务必设置）
WEB_PASSWORD=your-secret
docker compose up -d
```

浏览器访问 `http://主机:8080`：

- **概览**：各站今日状态、连续签到天数、最新奖励与当前积分（鸡腿 / 铜币 / 积分 / 烧饼）
- **签到记录**：按站点 / 时间范围筛选，含每条记录的奖励与余额
- **Cookie 管理**：粘贴导入各站 Cookie（脱敏展示、写回 .env 并即时热生效），linux.sb / sb.sb 支持账号密码兜底
- **立即签到**：手动触发一轮签到（可只选某个站点 / 强制重签）

## 部署

### 方式一：VPS + Docker（推荐）

```bash
docker compose build
docker compose run --rm checkin python main.py --test-notify
docker compose up -d          # 常驻模式，每 30 分钟一轮，靠 state.db 当日去重
```

或者在宿主机挂 cron 跑一次性任务（见 `crontab.example`）。

### 方式二：GitHub Actions

Fork 后在 `Settings → Secrets and variables → Actions` 里配置 `NS_COOKIE`、`V2EX_COOKIE`、
`LINUXSB_COOKIE`、`TG_BOT_TOKEN`、`TG_CHAT_ID`，`.github/workflows/daily.yml` 会按
北京 00:10 / 08:30 / 12:20 自动执行。

> Actions 的出口 IP 更容易吃 Cloudflare 挑战，建议在 Secrets 里加一个 `PROXY`。

### 方式三：本机 cron

见 `crontab.example`。

## 常用参数

| 参数 | 作用 |
|---|---|
| `--site nodeseek` | 只跑指定站点（可重复） |
| `--account 主号` | 只跑指定账号 |
| `--force` | 忽略当日已完成记录，强制再签 |
| `--dry-run` | 不发网络请求，只演练 |
| `--no-notify` | 不推送 |
| `--daemon --interval 1800` | 常驻，每 30 分钟一轮 |
| `--test-notify` | 只测通知链路 |
| `--doctor` | 环境体检：依赖 / 浏览器内核 / 配置 / 通知是否就位 |
| `--doctor --probe` | 额外探测各站点连通性与是否被 Cloudflare 拦 |

## 故障排查

先跑 `python main.py --doctor --probe`，大部分问题它能直接指出。

| 现象 | 处理 |
|---|---|
| NodeSeek 报「Cookie 已失效」 | 确认是整段复制（含 `session`），不要只取 `document.cookie` |
| 一直卡在 Cloudflare | 配 `PROXY`；把 `browser_backend` 明确设为 `camoufox`；`headless: false` 本地调试观察 |
| **浏览器通道报未找到后端 / 不可用** | 装 pip 包不算完，还要拉浏览器内核：`pip install -U camoufox && python -m camoufox fetch`（或 `pip install playwright && playwright install chromium`）。Docker 构建里若两者都失败，镜像会直接构建失败，不会带病出厂 |
| **`Failed to launch the browser process`（camoufox 启动失败）** | 两个最常见原因：① **缺系统库**——错误里会列出 `ldd` 缺失的 `.so`，在容器内 `apt-get install` 对应的库即可；② **ARM 架构**（如 aarch64 机器）——camoufox 官方内核仅 x86_64，会直接启动失败。本项目已支持 **auto 模式下自动降级到 Playwright Chromium**（原生支持 ARM），只要 `playwright install chromium` 过就没问题，无需改配置 |
| Camoufox 启动慢 / 报 UBO 下载失败 | 保留默认 `camoufox_exclude_addons: true`，跳过 uBlock Origin 下载 |
| 所有账号 cookie 都是空的 | 检查 `.env` 是否在**项目根目录**（非 `core/`），变量名是否与 config.yaml 对得上；`--doctor` 的「配置」段会显示每个账号的 cookie 长度 |
| V2EX 报「Cookie 已失效（A2）」 | V2EX 的 `A2` Cookie 有效期约 1 个月，过期就会这样。重新从浏览器复制整段 Cookie（含 `A2`）即可 |
| 烧饼报未取到 `_csrf` | 页面结构可能变化，或已被重定向到登录页，重新抓 Cookie |
| sb.sb 报 HTTP 404 / 地区封锁 | **多数不是路径错，是当前出口 IP 所在地区被站点封锁**（页面里写着「暂未对您所在地区开放」）。换到可访问的出口网络或配置 `runtime.proxy` 后再跑；若长期无法访问，可在 config.yaml 设 `sites.sbsb.enabled: false` 关掉该站 |
| sb.sb 报未找到签到入口 | 确认 cookie 有效；把日志里打印的按钮文案发出来对照，必要时改 `sites/sbsb.py` 的选择器 |
| 日志出现「状态库不可用」 | `data/` 目录不可写时会降级为内存去重，签到照常；不影响本次执行 |

- 站点间随机等待 30~120 秒、账号间 10~60 秒，失败重试带退避
- 只做签到，不做评论/点赞等会产生他人可见内容的操作（评论区互动有被举报风险）
- 每日结果都会推送，Cookie 失效立即告警，不会静默失败
- 仅供个人自动化使用，请遵守各站点规则
