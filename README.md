# Kaggle Team Radar

从环境变量读取一组 Kaggle team name，扫描 Kaggle 的公开比赛目录，匹配完整 leaderboard，并生成一个可部署到 GitHub Pages 的聚合榜单。

页面只保留至少命中一个目标团队的比赛，展示：

- Kaggle CSV 的官方 `Rank`
- `Top% = Rank ÷ 该榜单正排名队伍数 × 100`
- Kaggle 原样分数字符串（不跨比赛比较）
- Public / Private leaderboard 类型与比赛状态
- 在 Kaggle 标记 `awards_points=true` 时估算的奖牌候选区
- 扫描覆盖、失败数和截断状态，避免把部分结果伪装成完整结果

## 为什么选择 Hugo

本站使用 [Hugo](https://gohugo.io/) `0.164.0`。它是主流的轻量静态站点生成器，使用单个预编译二进制，原生读取 JSON 数据，并有官方的 [GitHub Pages 部署指南](https://gohugo.io/host-and-deploy/host-on-github-pages/)。本项目没有引入主题、Node、Ruby、远程字体或 CDN 脚本。

数据链路很简单：

```text
Kaggle API / leaderboard ZIP
            │
            ▼
Python：过滤、匹配、计算、字段白名单
            │
            ▼
data/leaderboard.json（生成文件，不提交）
            │
            ▼
Hugo：只负责渲染
            │
            ▼
public/（生成文件，不提交）→ GitHub Pages
```

## 本地运行

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Hugo 0.164.0。

```bash
uv sync --locked
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

```dotenv
KAGGLE_TEAMS=["Exact Kaggle Team A", "Exact Kaggle Team B"]
KAGGLE_API_TOKEN=your-token
KAGGLE_SCAN_WORKERS=2
KAGGLE_REQUEST_INTERVAL_SECONDS=2
```

`KAGGLE_TEAMS` 推荐写成 JSON 数组，这样 team name 中可以安全包含逗号。匹配会去除首尾空白，并做 Unicode NFKC 和大小写归一化；输出仍使用配置中的名称。

先用少量比赛验证凭据和页面：

```bash
uv run python -m agentkaggle_leaderboard --max-competitions 20
hugo server
```

完整扫描时去掉上限：

```bash
uv run python -m agentkaggle_leaderboard
hugo --gc --minify
```

默认输出为 `data/leaderboard.json`。原始 ZIP 只存在于进程临时目录，解析完成即删除。

## GitHub Pages 自动刷新

在公开仓库的 Settings → Secrets and variables → Actions 中添加两个 Repository secrets，名称必须与程序环境变量完全相同：

- `KAGGLE_API_TOKEN`
- `KAGGLE_TEAMS`

然后在 Settings → Pages 中把 Source 设为 **GitHub Actions**。

[Pages workflow](.github/workflows/pages.yml) 会在以下情况执行：

- push 到 `main`
- 每天 `02:17 UTC`（新加坡时间 `10:17`）
- 手动 `workflow_dispatch`

Secret 只注入数据生成步骤；Hugo、Pages 上传和部署步骤拿不到 Kaggle token。工作流使用只读仓库权限、Pages OIDC、固定版本和固定 action commit SHA。Pull request 只运行 [无密钥 CI](.github/workflows/ci.yml)，用合成数据验证 Python、Hugo 和公开产物扫描，不使用 `pull_request_target`，也不读取 secrets。

> `KAGGLE_TEAMS` 可以作为 Secret 隐藏仓库中的输入配置，但 team name 会出现在最终静态 HTML 中。公开 GitHub Pages 与“team name 本身保密”无法同时成立。

## 扫描和数据口径

### 公开目录边界

程序只枚举 Kaggle 的 `general` 和 `community` 公开组，使用 API 返回的 `next_page_token` 走完整目录并跨页去重；同时显式排除凭据相关的 `entered`、`hosted`、`unlaunched` 与 `unlaunched_community`。这是公开仓库的 fail-closed 选择：默认不发布受邀或尚未公开比赛的名称和结果。

Kaggle 对部分比赛可能返回 403、404、429 或不完整响应；例如仅用于提交论文、没有独立可下载 leaderboard 的赛道也可能出现在 competition 目录中。程序会低并发、限速、把 `Retry-After` 作为所有扫描线程共享的冷却期并重试；仍失败的比赛只计入固定的安全错误类别，页面会区分 404、拒绝访问、限流、网络与格式错误，不把原始异常、请求头或响应写入页面。只要存在未读取比赛或使用了 `--max-competitions`，页面就标记为 `partial`。如果可用比赛不足发现数的 50%，构建直接失败，GitHub Pages 会保留上一版而不会被严重残缺快照覆盖。

### 排名与百分比

完整 leaderboard ZIP 当前包含 `Rank, TeamId, TeamName, LastSubmissionDate, Score, SubmissionCount, TeamMemberUserNames`。程序只读取并输出需求所需字段；`Rank=0` 的 benchmark 行会被排除。

百分比位次使用同一 CSV 中 `Rank > 0` 的行数作分母，而不是可能不同步的 competition 元数据 `teamCount`。页面仍保留两个计数用于测试和审计。

### 奖牌候选

只有 Kaggle 返回 `awards_points=true` 时才计算候选区；否则显示“不计奖牌”。阈值按 Kaggle progression 表向下取整：

| 正排名队伍数 | 金牌区 | 银牌区 | 铜牌区 |
| --- | ---: | ---: | ---: |
| 1–99 | Top 10% | Top 20% | Top 40% |
| 100–249 | Top 10 | Top 20% | Top 40% |
| 250–999 | Top `10 + 0.2%` | Top 50 | Top 100 |
| 1000+ | Top `10 + 0.2%` | Top 5% | Top 10% |

这仍只是“排名区间候选”，不是正式奖牌。团队资格、取消资格、最终榜验证、比赛例外，以及进行中 Public LB 的变化都可能改变最终结果。Kaggle leaderboard API 不直接返回最终 medal。

## 隐私与安全设计

- `.env`、Kaggle 凭据文件、生成 JSON、`public/` 和浏览器 QA 产物均已加入 `.gitignore`。
- Python 输出结构使用字段白名单，不包含 `TeamMemberUserNames`、用户名列表、原始响应或认证信息。
- 活动比赛若意外返回 private leaderboard，构建会拒绝发布该比赛。
- 构建后会扫描允许的文本产物，拒绝 token 值、凭据变量名、原始成员字段、`.env`、CSV、ZIP、日志、未知文件类型和符号链接。
- 不启用 Kaggle SDK 的 `VERBOSE` / `VERBOSE_OUTPUT`，避免请求头进入日志。
- GitHub Actions 第三方步骤固定到完整 commit SHA，checkout 不保留 token；build 与 deploy 使用各自最小 job 权限。Hugo 二进制固定版本并校验 SHA-256。
- Pages artifact 只上传 `public/`，不会上传工作目录、原始 ZIP 或 `.env`。

建议 CI 使用一个没有比赛 host/admin 权限的专用 Kaggle 账号，进一步缩小 token 权限面。

## 测试

```bash
uv run python -m unittest discover -v
mkdir -p data
cp tests/fixtures/leaderboard.json data/leaderboard.json
hugo --gc --minify
uv run python scripts/check_public_artifact.py public data/leaderboard.json
```

测试覆盖环境解析、Unicode team 匹配、官方 Rank、benchmark 排除、四档奖牌阈值、公开目录分组、显式认证、共享限流冷却、严重降级拒绝、部分扫描状态和敏感产物扫描。浏览器验收使用合成 team name，避免把本地目标列表写入测试截图或日志。

## 目录

```text
agentkaggle_leaderboard/  # Kaggle 读取、聚合、口径与安全输出
assets/                   # 无第三方请求的 CSS / JavaScript
layouts/                  # Hugo 首页模板
scripts/                  # Pages 产物泄漏检查
tests/                    # 单元测试与合成榜单 fixture
.github/workflows/        # PR CI 与定时 Pages 部署
```

官方参考：[Kaggle CLI 认证](https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md)、[competition 命令](https://github.com/Kaggle/kaggle-cli/blob/main/docs/competitions.md)、[Hugo JSON data source](https://gohugo.io/content-management/data-sources/)。
