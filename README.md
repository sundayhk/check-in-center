# signin-hub

多站点自动签到系统：NodeSeek（含随机签到 + Cloudflare）、V2EX、烧饼社区（linux.sb）。
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
export $(grep -v '^#' .env | xargs)   # 本地直接 export；Docker 会自动读 .env

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
- 基本无 Cloudflare，L1 直连搞定

### 烧饼社区 linux.sb

- `GET /daily_checkin` 取 `_csrf` → `POST /daily_checkin`（`_csrf=xxx`）
- 登录态 cookie 是 `bbs_auth`，CSRF 取页面隐藏域或 cookie `bbs_csrf`
- 2026-08 起**间歇性**开 Cloudflare 托管挑战：L1 被拦会自动升级到浏览器通道
- Cookie 失效时，若配置了 `username/password`，会自动登录（页面算术验证码由脚本解、PoW 由页面 JS 算）
  ；若触发邮箱验证风控，只能人工在本地浏览器过一次再更新 Cookie
- 多账号 cookie 用 `&` 分隔

## 部署

### 方式一：VPS + Docker（推荐）

```bash
docker compose build
docker compose run --rm signin python main.py --test-notify
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

## 风控与合规

- 站点间随机等待 30~120 秒、账号间 10~60 秒，失败重试带退避
- 只做签到，不做评论/点赞等会产生他人可见内容的操作（评论区互动有被举报风险）
- 每日结果都会推送，Cookie 失效立即告警，不会静默失败
- 仅供个人自动化使用，请遵守各站点规则

## 故障排查

| 现象 | 处理 |
|---|---|
| NodeSeek 报「Cookie 已失效」 | 确认是整段复制（含 `session`），不要只取 `document.cookie` |
| 一直卡在 Cloudflare | 配 `PROXY`；把 `browser_backend` 明确设为 `camoufox`；`headless: false` 本地调试观察 |
| 浏览器通道报未找到后端 | `pip install camoufox && python -m camoufox fetch` |
| V2EX 找不到领取链接 | 可能早于 08:00 或今日已领；加 `--force` 复测 |
| 烧饼报未取到 `_csrf` | 页面结构可能变化，或已被重定向到登录页，重新抓 Cookie |
