# gate_check

参考 [hezhanleiok/gate](https://github.com/hezhanleiok/gate) 的 VPN Gate SSTP 节点检测项目：自动抓取节点，通过你部署的 Cloudflare Worker 检测，生成供 edgetunnel 使用的 `nodes.txt`，并发布到 GitHub Pages。

入口、Worker 域名等个人配置放在 **GitHub Actions Secrets** 中，不需要修改 Python 或工作流。可选运行参数通过 **Actions Variables** 调整。公开站点只发布节点清单，没有展示网页或 `data.json`；诊断报告保存在 Actions 产物中。

## 工作流程

```text
VPN Gate 官方 API（失败时回退 GitHub 镜像）
  → 筛选 TCP 节点 → 去重 → Worker 并发检测
  → 按国家、住宅／机房、延迟排序 → nodes.txt → GitHub Pages
```

每 2 小时的第 17 分钟触发一次（UTC 00:17、02:17、04:17 等），避开整点高峰，也可手动运行。GitHub 定时任务可能延迟，不保证精确到点更新。默认并发 32、连接超时 10 秒、读取超时 90 秒；临时故障最多重试 2 次，整轮检测的总时间预算为 900 秒。

## 部署

### 1. 准备检测 Worker

先按 [cm-Workers-CheckSocks5](https://github.com/lsh8848/cm-Workers-CheckSocks5) 的说明部署检测 Worker，确认支持 `/check?sstp=vpn:vpn@节点:端口`，响应中包含布尔类型的 `success`。

记下检测 Worker 的域名，例如 `check.example.com` 或 `my-check.example.workers.dev`。这里使用的是**检测 Worker 域名**。edgetunnel 的 UUID、管理员密码和节点域名仍在 edgetunnel 自己的后台配置。

### 2. 配置仓库 Secrets

Fork 本仓库或将代码推送到自己的仓库，进入：

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

| Secret 名称 | 必填 | 内容示例 | 说明 |
| --- | --- | --- | --- |
| `EDGE_HOSTS` | 是 | `a.example.com:443,b.example.com:443` | 填写你实测可用的入口地址，逗号或换行分隔；必须带端口 |
| `WORKER_DOMAIN` | 是 | `check.example.com` | 只填域名，不带 `https://`、端口、斜杠或检测路径 |
| `NODES_GITHUB_USERNAME` | 否 | `your-name` | 只填 GitHub 用户名或组织名；不填则自动使用当前仓库所有者 |

示例地址仅用于说明格式，请替换为自己的值。`EDGE_HOSTS` 也支持 IPv4 和带方括号的 IPv6，例如 `192.0.2.1:443`、`[2001:db8::1]:443`。空白会被清理，重复入口会按首次出现的顺序去重。

程序自动拼接以下地址：

```text
检测地址：https://<WORKER_DOMAIN>/check?sstp=vpn:vpn@
清单地址：https://<用户名>.github.io/<当前仓库名>/nodes.txt
```

通常只需填写前两个 Secrets。用户名覆盖只改变生成的订阅地址提示，不会把 Pages 部署到另一个账号，因此应与实际 Pages 所属账号一致。仓库名由 Actions 自动提供，无需配置。

本项目使用标准的 GitHub 项目 Pages 地址，不支持通过以上配置指定自定义 Pages 域名。Secrets 保存配置，但发布的 `nodes.txt` 是公开文件，其中入口地址和 SSTP 节点地址也会公开；检测 Worker 地址不会写入清单或日志。

### 可选：运行参数与筛选

进入 **Settings → Secrets and variables → Actions → Variables** 添加以下变量。全部可省略或留空，程序会使用默认值；三个核心配置仍使用 Secrets。

| Variable | 默认值 | 范围与作用 |
| --- | --- | --- |
| `CHECK_CONCURRENCY` | `32` | 1–64，并发检测数 |
| `CONNECT_TIMEOUT` | `10` | 1–60 秒，建立连接超时 |
| `CHECK_TIMEOUT` | `90` | 1–180 秒，Worker 读取超时 |
| `HTTP_TIMEOUT` | `30` | 1–120 秒，数据源读取超时 |
| `CHECK_RETRIES` | `2` | 0–3，临时网络错误的额外尝试次数，也用于数据源 |
| `RETRY_BACKOFF` | `1` | 0–30 秒，指数退避的起始等待时间；服务端 `Retry-After` 优先 |
| `RUN_TIMEOUT` | `900` | 10–1500 秒，整轮抓取、检测和入口探测的进程级总时间预算 |
| `MAX_CHECK_NODES` | `0` | 0–10000，最多检测的去重候选节点数；0 表示不限 |
| `ALLOWED_COUNTRIES` | 空 | 保留的两位国家代码，如 `JP,US,KR`；空表示不限 |
| `RESIDENTIAL_ONLY` | `false` | 只保留判断为住宅的 SSTP 节点 |
| `MAX_LATENCY_MS` | `0` | 0–180000，SSTP 延迟上限；0 表示不限，启用后排除未知延迟 |
| `MAX_PER_COUNTRY` | `0` | 0–10000，每个国家最多保留的 SSTP 节点数；0 表示不限 |
| `PROBE_EDGES` | `true` | 对入口进行 TCP 连接探测，仅用于诊断报告，不删除或重排入口 |
| `STALE_HOURS` | `6` | 1–720，距离上次成功检测并验证超过此时长，在运行摘要中提示可能过期 |

国家、住宅和延迟筛选在检测后进行，每国数量限制在排序后应用。`MAX_CHECK_NODES` 则在检测前截取数据源顺序中的前 N 个去重节点。所有筛选仅影响 SSTP 行，Secrets 中的独立入口始终完整保留；若筛选后没有 SSTP 节点，仍不覆盖旧清单。

### 3. 启用 Pages 并运行

1. 在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
2. 在 **Actions** 页面启用工作流；Fork 后需确认定时工作流已启用。
3. 选择 **VPN Gate Node Check → Run workflow**，使用默认分支运行。
4. 等待 `check` 作业完成后，访问 `https://你的用户名.github.io/你的仓库名/nodes.txt`，并查看运行摘要。

以当前仓库 `vvqc/gate_check` 为例，地址为 `https://vvqc.github.io/gate_check/nodes.txt`。项目没有首页，请直接打开 `/nodes.txt`。

配置方法见 [Secrets 官方文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets) 和 [Pages 官方文档](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

### 4. 接入 edgetunnel

将清单地址填入 edgetunnel 后台的「自定义优选IP」框并保存，之后刷新订阅即可使用更新后的节点。清单无注释头，先将 Secrets 中的全部 `EDGE_HOSTS` 各自单独写一行，再追加通过检测的 SSTP 节点行：

```text
a.example.com:443
b.example.com:443
a.example.com:443#日本-住宅-01$sstp://vpn:vpn@vpn12345.opengw.net:443
```

独立入口按配置顺序输出，清理空白并去重；即使 SSTP 节点数少于入口数，也会列出全部入口。SSTP 节点行中的入口继续按配置顺序轮换，住宅节点优先，同组按延迟排序。住宅／机房是根据 Worker 出口信息及主机名估算的；无法识别的节点单独标注“未识别”，不再归为机房。

## 失败与更新行为

- 配置缺失或格式不正确：联网前报错，提示配置项名称，不打印配置值。
- 官方源与镜像都失败、无 TCP 节点、Worker 全部异常或可用节点为零：工作流失败，不上传和部署新产物，保留上次发布的清单。
- 部分节点通过检测：发布全部配置入口和可用的 SSTP 节点。没有可用 SSTP 节点时，不发布只有入口的清单，仍保留旧文件。首次运行失败时还没有旧清单，地址可能返回 404。
- 旧清单保留不代表旧节点仍然可用。排查 Actions 中的失败原因、更新 Secrets 后，手动重新运行。
- 修改 Secrets 会在下一次运行时生效，无需提交代码；推送和 PR 只运行离线测试，不触发节点发布。

主源有记录但解析不出有效 SSTP 候选节点时，也会回退镜像。连接／读取超时、429 以及 500、502、503、504 会有限重试；鉴权错误、TLS 校验错误、格式错误和明确不可用的节点不会反复重试。超出总时间预算会终止整个检测子进程，工作流不进入发布步骤。

## 发布验证与运行报告

- 生成成功后，先读取线上 `nodes.txt` 比较完整内容；完全相同则跳过上传和部署，但记录本轮验证成功。
- 内容变化或旧文件不可用时执行部署，之后最多进行 6 轮内容验证，轮次间等待 3 秒，并在总请求预算内处理临时错误。若始终不一致或无法读取，任务标记失败。
- “检测失败未发布”和“已经部署但验证失败”会分开记录。后者不代表站点自动回滚，需要查看报告排查。
- Actions 运行摘要包含数据源、候选／已检测／可用／筛选后数量、国家与网络类型分布、耗时、重试及失败分类、入口 TCP 探测结果。
- 入口探测仅反映 Actions 服务器的 TCP 连通性，不代表你的本地网络或完整代理链路可用；入口在报告中用序号显示，对应配置中的顺序。
- `gate-report-运行ID-尝试次数` 产物保存 `run.json` 和 `summary.md`，保留 90 天。下次运行从此前报告恢复最后成功验证时间、最后部署时间和连续失败次数；历史缺失或读取失败会明确标注“未知”，不会冒充零次失败。
- 未改变内容的成功运行会更新“成功验证时间”，不会更新“最后部署时间”。超过 `STALE_HOURS` 后在摘要提示过期，不自动清空你保留的旧清单。

工作流只从默认分支发布。GitHub 公开仓库长期无活动时可能停用定时任务；如果运行记录不再新增，请在 Actions 页面检查并重新启用。报告本身不额外发送邮件或聊天通知。

## 本地开发与测试

使用 Python 3.11 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
python scripts/install_actionlint.py
.tools/actionlint
```

代码使用 4 空格缩进、`snake_case` 函数名；`.editorconfig` 统一 UTF-8、LF 换行和缩进，Ruff 负责格式和静态检查。`install_actionlint.py` 支持 Linux x86_64/arm64，下载固定版本并校验 SHA-256。CI 在 Python 3.11 和 3.14 上运行测试，并单独检查工作流。

运行依赖及间接依赖已固定版本，Actions 固定提交 SHA；Dependabot 每周提出依赖更新 PR。提交前运行上述检查，并用 `git diff --check` 检查空白问题。

测试使用模拟网络响应，不需要 Secrets 或外部服务。实际生成清单前，在当前终端设置环境变量：

```bash
export EDGE_HOSTS='a.example.com:443,b.example.com:443'
export WORKER_DOMAIN='check.example.com'
export GITHUB_REPOSITORY='your-name/your-repo'
# 可选；省略时使用 GITHUB_REPOSITORY 中的所有者
export NODES_GITHUB_USERNAME='your-name'
python vpngate.py --check-config
python vpngate.py
```

将示例配置替换为真实值。`--check-config` 只校验配置，不联网。成功后公开目录只生成 `public/nodes.txt`，本地诊断报告写入 `reports/`；本地运行不部署 Pages。脚本直接读取环境变量，不自动加载 `.env` 文件。产物、虚拟环境、工具缓存和 `.env` 文件均不提交到 Git。

首次真实验收时，先运行一次默认分支工作流，确认报告中的“已部署且内容验证通过”；然后在 edgetunnel 刷新订阅，分别检查独立入口和 SSTP 节点能否实际使用。离线格式校验不能替代客户端连通性测试。

## 项目结构

```text
vpngate.py               抓取、检测、筛选、清单生成和总时间预算
gate_config.py           环境配置及校验
gate_data.py             国家名称及网络分类关键词
gate_http.py             请求重试、超时与线程连接池
gate_report.py           运行报告与 Actions 摘要
gate_delivery.py         内容比较、发布验证与历史状态恢复
tests/                  离线单元测试与流水线测试
scripts/                固定版本的检查工具安装
.github/                发布、测试和 Dependabot 配置
requirements*.txt        已锁定的运行与开发依赖
public/nodes.txt         公开运行产物，不提交到 Git
reports/                诊断产物，不发布到 Pages、不提交到 Git
```

## 参考与致谢

- [hezhanleiok/gate](https://github.com/hezhanleiok/gate)：参考抓取、分类和 edgetunnel 清单格式。
- [VPN Gate](https://www.vpngate.net/)：节点数据源。
- [fdciabdul/Vpngate-Scraper-API](https://github.com/fdciabdul/Vpngate-Scraper-API)：备用节点数据源。
- [lsh8848/cm-Workers-CheckSocks5](https://github.com/lsh8848/cm-Workers-CheckSocks5)：检测 Worker。
- [cmliu/edgetunnel](https://github.com/cmliu/edgetunnel)：节点清单使用端。
