# gate_check

在 **GitHub Actions** 上抓取 VPN Gate 节点，通过你自己的 Cloudflare Worker 检测 SSTP 可用性，再把清单发布到 GitHub Pages，供 edgetunnel 订阅。

只需要配置入口列表和检测 Worker 域名。本地不安装依赖、不运行检测；公开站点只有 `nodes.txt`，运行报告留在 Actions 中。

```text
VPN Gate 数据源 → 解析 TCP 候选并去重 → Worker 检测 → 筛选可用节点
                                                        ↓
你的 EDGE_HOSTS ────────────────────────────────→ nodes.txt → GitHub Pages
```

## 快速部署

### 1. 准备检测 Worker

先部署 [cm-Workers-CheckSocks5](https://github.com/lsh8848/cm-Workers-CheckSocks5)，确认它支持 `/check?sstp=vpn:vpn@主机:端口`，并返回包含布尔字段 `success` 的 JSON。

记下**检测 Worker** 的域名，例如 `check.example.com`。edgetunnel 的节点域名、UUID、管理员密码是另一套配置，仍在 edgetunnel 后台管理。

### 2. 填写仓库 Secrets

Fork 本仓库，进入 **Settings → Secrets and variables → Actions → Secrets → New repository secret**：

| Secret | 是否必填 | 填什么 |
| --- | --- | --- |
| `EDGE_HOSTS` | 必填 | 你实测可用的入口，例如 `a.example.com:443,b.example.com:443`；支持逗号或换行分隔，每项必须带端口 |
| `WORKER_DOMAIN` | 必填 | 仅检测 Worker 域名，例如 `check.example.com`；不要加 `https://`、路径、端口或查询参数 |
| `NODES_GITHUB_USERNAME` | 通常不用填 | 默认采用当前仓库所有者；需要覆盖时只填 GitHub 用户名或组织名 |

上面的地址都是格式示例，请替换成自己的值。入口也支持 IPv4 和带方括号的 IPv6，例如 `192.0.2.1:443`、`[2001:db8::1]:443`。程序会清理空白、统一格式并按原顺序去重。

完整地址由程序拼接：

```text
Worker：https://<WORKER_DOMAIN>/check?sstp=vpn:vpn@
清单：  https://<仓库所有者>.github.io/<仓库名>/nodes.txt
```

`NODES_GITHUB_USERNAME` 只影响程序提示的清单地址，不会把 Pages 部署到另一个账号。通常保留默认值即可，仓库名无需配置。本项目的配置方式按标准 GitHub 项目 Pages 地址设计。

### 3. 开启 Pages 并运行一次

1. 在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
2. 在 **Actions** 页面启用工作流，选择 **VPN Gate Node Check**。
3. 点击 **Run workflow**，选择默认分支运行。
4. 运行结束后，确认摘要显示 **“已部署且内容验证通过”** 或 **“内容相同，已验证并跳过部署”**。
5. 打开 `https://你的用户名.github.io/你的仓库名/nodes.txt`。

本仓库的地址是 [vvqc.github.io/gate_check/nodes.txt](https://vvqc.github.io/gate_check/nodes.txt)。Fork 后请使用你自己仓库的地址。项目没有展示首页，直接访问 `/nodes.txt`。

### 4. 接入 edgetunnel

把上述清单 URL 填入 edgetunnel 后台的 **「自定义优选IP」** 框，保存并刷新订阅，再在客户端验证节点连通性。

**Tests 工作流通过，只表示代码检查通过；真实检测和发布要看 VPN Gate Node Check。** Worker 检测成功也只代表检测当时可用，不能保证下一次刷新前始终在线。

## TXT 里有哪些内容？

文件没有注释头，包含两类行：

```text
a.example.com:443
b.example.com:443
a.example.com:443#日本-住宅-01$sstp://vpn:vpn@vpn12345.opengw.net:443
b.example.com:443#韩国-未识别-01$sstp://vpn:vpn@vpn67890.opengw.net:443
```

- **前面的独立入口行**：来自 `EDGE_HOSTS`，每个去重后的入口单独一行，按配置顺序完整保留。
- **后面的 SSTP 行**：来自本轮检测通过且符合筛选条件的 VPN Gate 节点；入口从 `EDGE_HOSTS` 中轮换使用。

因此，**TXT 总行数 = 独立入口数 + 最终 SSTP 节点数**。增加 `EDGE_HOSTS` 会增加入口行，不会增加 VPN Gate 的 SSTP 节点数量。

住宅节点优先，其后结合延迟排序。`住宅`、`机房`、`未识别` 是基于 Worker 出口信息和主机名的估算标签，不是严格的网络类型认证。入口地址和 SSTP 地址会随 TXT 公开；Secrets 用于保存配置，不会让公开清单中的入口保密。

## 节点为什么会少？

先看运行报告里的数字，定位节点在哪一步减少。不要只看 TXT 的总行数，也不要把 VPN Gate 网站显示的服务器总量当成最终 SSTP 数量。

| 字段 | 表示什么 | 数量减少的常见位置 |
| --- | --- | --- |
| `raw_nodes` | 本次数据源返回并解析出的原始记录数 | 本次数据源本身只返回了这些记录 |
| `candidates` | 解析、去重并应用检测数量上限后的候选数 | 非 TCP 配置、配置或地址无效、重复记录、`MAX_CHECK_NODES` 限制 |
| `checked` | 已完成检测的候选数 | 若少于候选数，检查是否耗尽总时间预算或任务被中断 |
| `available` | Worker 返回 `success: true` 的数量 | 其余节点被判为不可用，或遇到了请求／响应错误 |
| `selected` | 应用筛选后实际写入 TXT 的 SSTP 数量 | 国家、住宅类型、延迟、每国数量筛选 |
| `edge_count` | 去重后的独立入口数 | 来自你的配置，与 VPN Gate 检测通过数分开计算 |

**当前实现如何找候选？** 它从数据源提供的 OpenVPN 配置中解析 TCP 条目及端口，再交给 Worker 验证 SSTP。它不是直接复制 VPN Gate 全站节点，也不承诺覆盖全站所有 SSTP 入口。主源无法提供有效候选时才回退镜像；当前不会把两个数据源合并扩充。

**默认有没有限制数量？** 没有。未配置 Variables 时，不限制检测数量、不限制国家、不只保留住宅、不设置延迟上限，也不限制每国数量。例如 `available = 44`、`selected = 44` 就表示通过检测的 44 个节点全部写入了清单。

**`node_unavailable` 是什么？** 表示 Worker 正常返回了 `success: false`，不是被筛选变量删除。报告目前不保留 Worker 的原始错误文本，因此仅凭这个计数不能判断具体是节点端口、协议、认证还是网络问题。

**怎样查看完整报告？** 打开对应的 **VPN Gate Node Check** 运行页面查看摘要；需要 `raw_nodes` 等完整字段时，下载下方名为 `gate-report-运行ID-尝试次数` 的 artifact，查看 `run.json` 或 `summary.md`。

## 可选配置：筛选与运行参数

进入 **Settings → Secrets and variables → Actions → Variables** 添加。全部可留空使用默认值，日常运行只配置前面的两个必填 Secrets 即可。

### 影响节点数量的参数

| Variable | 默认值 | 作用 |
| --- | --- | --- |
| `MAX_CHECK_NODES` | `0` | 最多检测多少个去重候选；0 表示不限，允许 0–10000 |
| `ALLOWED_COUNTRIES` | 空 | 只保留指定国家代码，例如 `JP,KR,US`；空表示不限 |
| `RESIDENTIAL_ONLY` | `false` | 设为 `true` 后只保留判断为住宅的 SSTP 节点 |
| `MAX_LATENCY_MS` | `0` | 延迟上限，单位毫秒；0 表示不限，允许 0–180000；启用后会排除未知延迟 |
| `MAX_PER_COUNTRY` | `0` | 每个国家最多保留多少个 SSTP 节点；0 表示不限，允许 0–10000 |

`MAX_CHECK_NODES` 在检测前按数据源顺序截取。其余筛选在检测后应用，每国数量限制在排序后应用。筛选只影响 SSTP 行，独立入口仍完整保留；但如果筛选后没有任何 SSTP 节点，整轮不会发布新文件。

### 请求、时间预算与诊断

| Variable | 默认值 | 作用与范围 |
| --- | --- | --- |
| `CHECK_CONCURRENCY` | `32` | 并发检测数，1–64 |
| `CONNECT_TIMEOUT` | `10` | 建立连接超时，1–60 秒 |
| `CHECK_TIMEOUT` | `90` | Worker 读取超时，1–180 秒 |
| `HTTP_TIMEOUT` | `30` | 数据源读取超时，1–120 秒 |
| `CHECK_RETRIES` | `2` | 临时请求错误的额外尝试次数，0–3，也用于数据源 |
| `RETRY_BACKOFF` | `1` | 指数退避的起始等待时间，0–30 秒；服务端 `Retry-After` 优先 |
| `RUN_TIMEOUT` | `900` | 整轮抓取、检测和入口探测的总时间预算，10–1500 秒 |
| `PROBE_EDGES` | `true` | 是否对入口做 TCP 探测，只出报告，不删减或重排入口 |
| `STALE_HOURS` | `6` | 距上次成功检测并验证超过多少小时提示可能过期，1–720 |

重试只针对连接／读取超时、429，以及 500、502、503、504 等临时错误。Worker 明确返回不可用、鉴权错误、TLS 校验错误和响应格式错误不会被反复重试。单纯提高重试或并发，不能保证节点更多。

入口探测反映的是 **Actions 服务器到入口的 TCP 连通性**，不代表你的本地网络或完整代理链路可用。报告中的入口序号对应配置顺序。

## 自动更新与失败处理

- **定时**：每两小时的第 17 分钟运行一次，cron 为 `17 */2 * * *`，按 UTC 解释；GitHub 调度可能延迟。
- **手动**：修改 Secrets 或 Variables 后，可直接 Run workflow，无需提交代码。发布仅允许默认分支。
- **内容未变**：比较线上 TXT 后跳过上传、部署，仍记录本轮验证成功。
- **内容变化**：部署后再次核对线上文件，最多进行 6 轮验证，轮次间等待 3 秒，并受请求预算约束。
- **检测失败或无可用 SSTP**：不发布新文件，保留上次清单；也不会改成只发布独立入口。
- **已经部署但验证失败**：任务标记失败，报告会明确区分；这不代表站点自动回滚。

保留旧文件不代表其中节点仍然在线。报告记录最后成功验证时间、最后部署时间和连续失败次数；超过 `STALE_HOURS` 后提示可能过期。内容未变的成功运行只更新验证时间，不更新部署时间。

报告保留 90 天。历史报告缺失或读取失败时，时间和失败次数会标为未知，不会伪装成零次失败。若长期没有新增运行记录，应检查工作流是否被 GitHub 停用。

## 常见排查

| 现象 | 先检查 |
| --- | --- |
| 缺少配置、域名格式错误 | 核心配置是否放在 **Secrets**；`WORKER_DOMAIN` 是否只填域名 |
| `Pages 配置` 失败 | Settings → Pages 的 Source 是否选择 **GitHub Actions** |
| 大量 `node_unavailable` | Worker 正常响应，但判定节点不可用；与国家／数量筛选是两回事 |
| 大量 `http_429`、连接或读取超时 | 查看错误分类与重试统计，再评估并发、Worker 状态和网络情况 |
| `available` 明显大于 `selected` | 检查国家、住宅、延迟及每国数量筛选 |
| TXT 没更新 | 看最后一次节点检测运行是否成功、是否内容相同而跳过部署、是否仍在使用旧清单 |
| 客户端数量与 TXT 不同 | 先确认实际订阅的 URL，再检查 edgetunnel 的解析、去重和订阅设置；TXT 的入口行不等于 SSTP 节点 |

## 维护方式

本项目只在 GitHub Actions 中安装依赖、运行测试和检测。本地仅编辑源码和执行 Git 操作，不创建虚拟环境、不下载检查工具。

推送或 PR 会自动触发 **Tests**：检查 Python 3.11／3.14、Ruff 和 actionlint。运行依赖已锁定版本，Actions 固定提交 SHA，Dependabot 每周提出更新 PR。

主要代码：`vpngate.py` 负责检测和生成，`gate_config.py` 负责配置，`gate_http.py` 负责请求，`gate_report.py` 负责报告，`gate_delivery.py` 负责历史恢复、内容比较和发布验证。公开产物为 `public/nodes.txt`；`reports/` 仅用于 Actions 报告，不发布到 Pages。

## 参考项目

- [hezhanleiok/gate](https://github.com/hezhanleiok/gate)：参考实现与节点格式。
- [VPN Gate](https://www.vpngate.net/)：节点数据源。
- [Vpngate-Scraper-API](https://github.com/fdciabdul/Vpngate-Scraper-API)：备用数据源。
- [cm-Workers-CheckSocks5](https://github.com/lsh8848/cm-Workers-CheckSocks5)：检测 Worker。
- [cmliu/edgetunnel](https://github.com/cmliu/edgetunnel)：清单使用端。
