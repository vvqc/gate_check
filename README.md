# gate_check

参考 [hezhanleiok/gate](https://github.com/hezhanleiok/gate) 的 VPN Gate SSTP 节点检测项目：自动抓取节点，通过你部署的 Cloudflare Worker 检测，生成供 edgetunnel 使用的 `nodes.txt`，并发布到 GitHub Pages。

个人配置全部放在 **GitHub Actions Secrets** 中，不需要修改 Python 或工作流。只发布节点清单，没有展示网页或 `data.json`。

## 工作流程

```text
VPN Gate 官方 API（失败时回退 GitHub 镜像）
  → 筛选 TCP 节点 → 去重 → Worker 并发检测
  → 按国家、住宅／机房、延迟排序 → nodes.txt → GitHub Pages
```

每 2 小时整点触发一次（UTC 00:00、02:00、04:00 等），也可手动运行。GitHub 定时任务可能延迟，不保证精确到点更新。每个节点只检测一次，默认并发 32、单次检测超时 90 秒。

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

### 3. 启用 Pages 并运行

1. 在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
2. 在 **Actions** 页面启用工作流；Fork 后需确认定时工作流已启用。
3. 选择 **VPN Gate Node Check → Run workflow**，使用默认分支运行。
4. 等待 `build` 和 `deploy` 成功后，访问 `https://你的用户名.github.io/你的仓库名/nodes.txt`。

以当前仓库 `vvqc/gate_check` 为例，地址为 `https://vvqc.github.io/gate_check/nodes.txt`。项目没有首页，请直接打开 `/nodes.txt`。

配置方法见 [Secrets 官方文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets) 和 [Pages 官方文档](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

### 4. 接入 edgetunnel

将清单地址填入 edgetunnel 后台的「自定义优选IP」框并保存，之后刷新订阅即可使用更新后的节点。清单无注释头，先将 Secrets 中的全部 `EDGE_HOSTS` 各自单独写一行，再追加通过检测的 SSTP 节点行：

```text
a.example.com:443
b.example.com:443
a.example.com:443#日本-住宅-01$sstp://vpn:vpn@vpn12345.opengw.net:443
```

独立入口按配置顺序输出，清理空白并去重；即使 SSTP 节点数少于入口数，也会列出全部入口。SSTP 节点行中的入口继续按配置顺序轮换，住宅节点优先，同组按延迟排序。住宅／机房是根据 Worker 出口信息及主机名估算的；无法识别的节点沿用参考项目归入机房组，并非严格的网络类型判定。

## 失败与更新行为

- 配置缺失或格式不正确：联网前报错，提示配置项名称，不打印配置值。
- 官方源与镜像都失败、无 TCP 节点、Worker 全部异常或可用节点为零：工作流失败，不上传和部署新产物，保留上次发布的清单。
- 部分节点通过检测：发布全部配置入口和可用的 SSTP 节点。没有可用 SSTP 节点时，不发布只有入口的清单，仍保留旧文件。首次运行失败时还没有旧清单，地址可能返回 404。
- 旧清单保留不代表旧节点仍然可用。排查 Actions 中的失败原因、更新 Secrets 后，手动重新运行。
- 修改 Secrets 会在下一次运行时生效，无需提交代码；推送和 PR 只运行离线测试，不触发节点发布。

## 本地开发与测试

使用 Python 3.11 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

代码使用 4 空格缩进、`snake_case` 函数名；`.editorconfig` 统一 UTF-8、LF 换行和缩进。提交前运行上述测试，并用 `git diff --check` 检查空白问题。

测试使用模拟网络响应，不需要 Secrets 或外部服务。实际生成清单前，在当前终端设置环境变量：

```bash
export EDGE_HOSTS='a.example.com:443,b.example.com:443'
export WORKER_DOMAIN='check.example.com'
export GITHUB_REPOSITORY='your-name/your-repo'
# 可选；省略时使用 GITHUB_REPOSITORY 中的所有者
export NODES_GITHUB_USERNAME='your-name'
python vpngate.py
```

将示例配置替换为真实值。成功后只生成 `public/nodes.txt`；本地运行不部署 Pages。脚本直接读取环境变量，不自动加载 `.env` 文件。`public/`、虚拟环境和 `.env` 文件已加入忽略规则。

## 项目结构

```text
vpngate.py               配置校验、抓取、检测和清单生成
tests/                  离线单元测试与流水线测试
.github/workflows/      定时发布与推送／PR 测试
requirements.txt        Python 依赖
public/nodes.txt         运行产物，不提交到 Git
```

## 参考与致谢

- [hezhanleiok/gate](https://github.com/hezhanleiok/gate)：参考抓取、分类和 edgetunnel 清单格式。
- [VPN Gate](https://www.vpngate.net/)：节点数据源。
- [fdciabdul/Vpngate-Scraper-API](https://github.com/fdciabdul/Vpngate-Scraper-API)：备用节点数据源。
- [lsh8848/cm-Workers-CheckSocks5](https://github.com/lsh8848/cm-Workers-CheckSocks5)：检测 Worker。
- [cmliu/edgetunnel](https://github.com/cmliu/edgetunnel)：节点清单使用端。
