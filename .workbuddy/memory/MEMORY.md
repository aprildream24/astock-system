# astock-system 长期项目记忆

## 运行环境（重要）
- Bash 的报错只在 shell 启动脚本（缺 `dirname`/`ls`/`tail` 等 GNU coreutils）。
  **跑 python / 可执行文件用绝对路径完全正常**：
  `C:/Users/<用户>/.workbuddy/binaries/python/versions/3.13.12/python.exe -X utf8 xxx.py`。
  只有 `| tail`、`| head`、`ls` 这类外壳命令会失败——需要截断就让脚本自己少打印。
  PowerShell 亦可，但 stdout 不回显给工具结果 → 必须 `| Out-File -Encoding utf8` 再 Read。
- 中文输出会被 GBK 污染 → 用 `python -X utf8`（或 `-m unittest` 同理）。
- 跑单个测试套件：`python -X utf8 -m unittest discover -s tests -t tests -p "test_x.py" -v`
  （在仓库根目录直接 `-m unittest test_x` 会 ModuleNotFoundError，因为 tests 不在 sys.path）。
- Glob 结果有缓存，删除文件后仍返回旧条目；用 Read 确认真实状态。
- ⚠️ **别用 `python -c "...含反引号/中文长字符串..."` 写记忆**：bash 会先做命令替换，
  把反引号内容当命令执行。写文件用 Write/Edit 工具，不要塞进 bash 字符串。
- ⚠️ **C 盘写满时工具链会报 `ENOSPC`、`database or disk is full`、抓取 `rc=-9`**。
  这是环境故障不是代码 bug。**不要反复重试写操作**（会连锁破坏），先停下报告。
  2026-09-15 22:30 实测 C 盘 200GB 用尽、剩 0.01GB。
- ⚠️ **用户拒绝过全盘扫描**（`Get-ChildItem C:\ -Recurse` 命中
  `CodeBuddyExtension\Data\Public\auth` → 被沙箱拦）。查磁盘占用要限定窄目录，
  别扫整盘；系统盘清理须用户决策，不擅自删。

## 回归纪律
- 入口 `python tests/run_regression.py`（cwd 自动切到 tests）。ALL_PASS 才过。
- 规则：PASS 数只许涨不许跌，FAIL 必须 0。基线写在 `tests/baseline.json`。
- 2026-09-15 基线：**PASS=241 FAIL=0**（09-13 两轮 89→109→131；09-14 智谱 137；
  09-15 三阶段 162→189→196→197；第四轮 +e2e_safety(13)+source_failover(10)=220；
  第五轮 +gate_no_deadlock(12)+daily_check(9)=241；第六轮 +cloud_watch(26) → **267**）。
- **回归输出别写进仓库根目录**（`_r4.txt` 等会被 `test_deploy` 扫到 → 它被同步上
  CI → runner 上挂 → 零推送。真实踩过）。写 `%TEMP%`。

## ★★ 数据源会整片失效——单点源 = 定时炸弹（2026-09-15 两轮实盘事故）
### 第一轮（上午）：域名迁移 + 失效码
腾讯 K线裸域名 `ifzq.gtimg.cn` 返 **HTTP 501**（端点下线）。而熔断码只有
`(403,429,418)` ⇒ 501 被当普通失败重试 2 次+指数退避 ⇒ 单票白等 6–15 秒
⇒ 全市场拖爆 timeout ⇒ 第 8 步「构建+推送」skipped ⇒ 全天零推送。
修法：`TX_HOSTS` 候选列表 + `DEAD_CODES = (404,410,501)` 不重试且计入熔断。

### ★ 第二轮（晚间）：上一轮的结论**已被推翻**，勿再照抄
**当天实测：所有日K批量端点全线失效，只剩新浪一家。**
| 端点 | 实测 |
|---|---|
| `push2his.eastmoney.com` | RemoteDisconnected（不可达） |
| `web.ifzq.gtimg.cn` | **HTTP 501** ← 上一轮我认定的"正确答案"也挂了 |
| `ifzq.gtimg.cn` | HTTP 501 |
| **`quotes.sina.cn`** | **200 / 0.47s ← 唯一活源** |
| `money.finance.sina.com.cn` | **HTTP 456**（不耐压，压测后封 IP，**禁用**） |
| `qt.gtimg.cn` / `hq.sinajs.cn` | 200（快照可用） |

⇒ 教训：**"迁到备用域名"只是续命，必须建"多源 + 死源短路"的架构**。

### ★★ 真凶：死源短路缺失（吞吐被吃 50 倍）
旧 `kline_batch` 每票固定 `em→tx→sina`，失败开销 2~2.5s/票；12 并发槽全被
死源吃掉 ⇒ 全市场 **46 分钟**（卡死 CI 45min 超时）。
**修法**：`_skip()` 在 pull 入口检查熔断态，已 blocked 的源**不再发包**。
```python
def _skip(h_or_hs):
    if isinstance(h_or_hs, str): return guard(h_or_hs).blocked()
    return all(guard(h).blocked() for h in h_or_hs)
```
⚠️ **我自己引入又修掉的严重 bug**：加 `if em blocked and all(tx blocked): return {}`
→ 把"主源双死"误判成"全死"，**0.01s 返回空字典、0/4937、连 sina 兜底都不跑**。
**只有兜底源也拿不到才叫失败，绝不在入口提前放弃。**

### ★★ 并发/限速标定（唯一吞吐决定因素）
`RATE_PROFILES` 按 host 分档：sina 20/s(lo8/hi26)、money.finance.sina 6/s、
em/tx 10/s。`kline_batch(workers=20)`；**主源双死时单通道跑满 20 并发**
（原固定切两半，死通道的槽位在空转）。
- 实测 rate=20 w=20 → **0.145s/票 → 全市场 4937 只 ≈ 11.9 分钟**（原 46 分钟）。
- ⚠️ 我曾报"3.9 分钟"是**错的**——那是用 `money.finance.sina.com.cn` 压测得来的，
  该域名不耐压（456）。**压测本身会把源打死，别拿压测数字当产能。**

## ★★ 演练/测试工具绝不能在**生产目录**上做破坏性操作（2026-09-15 血案）
`tools/e2e_drill.py` 旧版把 config 私密文件暂存为 `config/<f>.e2e_bak`，
且恢复带 `not os.path.exists(p)` 守卫 ⇒ 演练中任何重建 p 的代码都会让
**真配置被永久遗弃**，生效的是假配置。后果：`notify.json`/`watch.json`/
`holdings.json` 全丢 → 本地 task 退化无凭据 dry-run →
**我拿自己造成的破坏当"没推送是正常的"证据**（自欺循环）。
- 修法：暂存挪到 `.e2e_staging/`（带 pid，不进 config）；`_restore`
  **无条件覆盖**；`_recover_orphans()` 启动自检；演练前后 config 状态
  **对账**，漂移即 FAIL。
- 复原工具：`tools/_restore_cfg.py`（一次性，可删）。
- 规则：**任何测试/演练若要改生产文件，必须先把工作根目录重定向到 tempdir**。
  `tests/test_e2e_safety.py` 就是靠这个把 drill 的 `ROOT` 指向沙箱。

## ★ rc=0 ≠ PASS（2026-09-15）
`pipeline.build` 数据未就绪时打印「拒绝构建」+发告警，然后 `return None`
——**退出码仍是 0**。判定必须三条件齐备：rc==0 + 不含「拒绝构建」
+ 有成功产出标志（见 `e2e_drill._judge`）。

## 实盘体检工具
- `tools/daily_check.py`：CI/数据/推送/站点 四查，硬指标（CI、推送）红即
  PROBLEM。推送查**远端账本优先**（用户常不开机，本地镜像不可信）。
- `tools/e2e_drill.py <task>`：逐 task 真跑（含抓取），判定见上。


## ★ CI 精确模拟（诊断"本机全绿、CI 必挂"的第一手法）
本仓库最值钱的排障手段，用它抓出过**两次"全天零推送"**：
```python
# 用 deploy.collect_files() 取入库文件 → 拷到空目录（无 config 私密文件）
# → 跑 tests/run_regression.py。CI 的失败会原样复现。
spec = importlib.util.spec_from_file_location('dep','tools/deploy.py')
dep = importlib.util.module_from_spec(spec); spec.loader.exec_module(dep)
ci = tempfile.mkdtemp()
for rel in dep.collect_files():
    dst = os.path.join(ci, rel); os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(rel, dst)
subprocess.run([sys.executable,'-X','utf8','tests/run_regression.py'], cwd=ci)
```
**已自动化为 `tests/test_ci_parity.py`**（剥离 config 私密文件后跑受影响子集）。
为什么必须做：CI 上没有 `config/{users,watch,holdings,notify}.json`（不入库），
依赖它们的测试**本机永远绿、CI 必挂**。

### 2026-09-15 第三次故障（第三个独立 bug）
第 5 步「回归自检」在 CI 必挂 → 后 6 步全 skipped。三个诱因：
1. 套件依赖 config 私密文件 → 通道为空 → `sent=False`，而两个用例裸断言 `sent=True`。
2. 根目录残留 `_deploy_out.txt` 被同步上 CI → `test_deploy` 扫到 FAIL。
   → 根目录改**白名单**（`ROOT_ALLOW`），不用"排除 `_` 前缀"（黑名单永远列不全）。
3. 我的 `test_ci_parity` spawn 整轮回归 → **无限递归**。
   → 只跑受影响子集 + `CI_PARITY_CHILD` 防递归。
另外：`test_ci_parity` 改名配置若被强杀会残留 `.ciparity_bak`（**真实弄坏过本机
config**）→ 必须 atexit + finally 双兜底。

### `tools/deploy.py` 两个致命 bug（都已修）
- `EXCLUDE_DIRS` 含 `.github/workflows` + 剪枝写 `not d.startswith(".git")`
  → **把 `.github` 一起剪掉** → `stock.yml` 永不上线（修复=白做）。只剪 `.git` 本身。
- `set_secret` 用了不存在的 `nacl.public.SecretBox` → 正确是 `SealedBox(pk).encrypt()`。
- 部署执行脚本：`tools/run_deploy.py`（token 从 `Temp/astock_gh_token.txt` 读入内存；
  **token 不能写进命令行文本**，沙箱会重写成 `zu-` 占位）。

## 「全天零推送」类故障排查清单（2026-09-15 事故固化）
用户问「为什么没收到推送」时**按此顺序查**，别瞎猜：
1. **先查 GitHub Actions runs**（`api.github.com/repos/aprildream24/
   astock-system/actions/runs`）看 conclusion；failure/cancelled 则用
   `/runs/{id}/jobs` 看**哪一步**挂 + 每步 started/completed 耗时。
2. 本项目历史模式：`第7步抓取挂 → 第8步构建+推送整步 skipped` ⇒ 零推送。
   根因通常是**抓取耗时 > workflow timeout**。
3. **两处静默点必须同时怀疑**（本次是两重故障叠加）：
   - `fetch_daily` 断档锚：若 `latest_td` 用「今日应达交易日」，当日数据未入库
     时全市场被判断档 → 走 days=260 全量 → 53 分钟超时。**锚必须取
     `max(库中最新日期, 上一交易日)`**；轻量任务全量兜底封顶 40 根。
   - `build()` 就绪闸门：`data_ready_for`/`is_trading_day_cross` 都以
     「指数日K含当日」为必要条件（日历源自指数K线）。**pre/auction 本就在
     当日收盘K线入库前运行** → 必须走 `_preauction_ready()` 专用闸门，
     否则这两个任务永不通过。仅 close/review 用收盘闸门。
4. **拒绝构建不得静默**：`_notify_data_blocked()` 必须发告警；抓取步骤加
   `continue-on-error: true`（失败不连坐推送）。
5. 性能锚：全量 4936 只实测 **≈53 分钟**（120 只 78 秒外推）。
   `timeout-minutes` 45 → 75。增量（days<=20）才应是常态。

## AI 叙事（智谱免费模型）
- `pipeline/narrative.py` GLM provider 默认 **glm-4.7-flash**（智谱免费档主力，
  输入输出 0 元）；`GLM_MODEL` 环境变量可覆盖。glm-4.5-flash 已 2026-01-30 下线，
  glm-4.6 是付费模型——都不许当默认。
- 密钥纪律：源码零密钥。env（GLM_API_KEY）优先，本地 `config/notify.json` 的
  `glm_api_key`/`glm_model` 兜底（key 已落盘，gh_sync EXCLUDE 含 notify.json 不会泄漏）。
  stock.yml 已把 `GLM_API_KEY`/`GLM_MODEL` 注入 close 与 review 两步（GitHub Secrets）。
- 已踩坑：glm-4.7-flash **思考模式默认开启** → 首响应可超 30s，必须
  `extra: {"thinking":{"type":"disabled"}}`；免费档偶发瞬时限流 429（退避即过）。
  `narrative._call` 超时已改 per-provider（glm=60s）。
- 真验已通过（2026-09-14）：原始调用 tokens=12 + narrate() 263 字叙事。
  工具：`python -X utf8 tools/test_glm.py <key>`。沙箱真验可行——key 从文件读
  （如 zcode-proxy/proxy.py）注入 env，别把 key 写进命令行文本（会被重写）。

## 三条不可回退的红线（2026-09-13 整改固化）1. **买区必须是可下单的窄带**。`entry_plan.now_zone` 宽度 ≤4.5%
   （`MAX_NOW_ZONE_WIDTH`）。曾经因为下沿未对收盘价约束，出现「买区 28.00~476.36」
   的跨越式伪区间——数学上 close 落在区内，但用户根本无法按价下单。
   **2026-09-14 第二漏洞源已堵**：区间池 detect_stage_bottom 买区 box*0.99~1.05
   恒宽 6.06%（91/127 现在买违规）→ engines.py 改围绕现价窄带 + build.py
   action 赋值后兜底闸（宽>4.5% 一律收窄）。⚠️ 新引擎造买区必须过这两道闸。
2. **引擎四态优先于买区判定**。`scoring._decide` 先看 `action_hint`
   （`ACTION_HINT_MAP`），不得把「过热/勿追」判成「现在买」。
   主推位只放 `action==现在买 且 dist_pct==0` 的票；其余进「等待更好买点」独立分组。
3. **扫描宇宙 = 快照 ∪ K线全历史**（`scan_universe`），不是「当日 klines 有行」。
   后者会让 K线同步慢一天的票永久消失在视野里。剔除必须全留痕，禁止静默 continue。
   覆盖面看 `build.LAST_SCAN_COVERAGE`（<90% 应补数）。

## 推送版面纪律
- 微信/PushPlus/邮件 webview 对 float/flex 支持极不稳定 → **一律用 `<table>` 对齐**。
- **版式为深色主题（2026-09-14 用户定调：不要白色底板，融入底色）**：
  根容器 `#15181e` / 卡片 `#1d222b` / 边框 `#2b313d`，浅色文字；三色语义
  买入 `#ff6b5e` / 卖出 `#4ecf8e` / 持有 `#6ab0ff` / 止损 `#ff8a80`。
  徽章底色（ACTION_BG/STATUS_CLS）保持饱和色+白字，**不许提亮**。
  必须显式写背景色——webview 深色模式把无背景容器渲染成透明。
- 纯文本通道（ServerChan）走 `notifier.html_to_text()` 结构化降级，不要剥标签。
- `_card()` 输出的 `<!--card-->` 是裁剪哨兵，`_clip_html` 依赖它按整卡回退。
- **可买票必须排在"等回踩/观望"前面**（2026-09-14 用户困惑整改）：高分等回踩票
  放首位会让用户第一眼看到"不能买"，与后面的✅买入矛盾。picks 已按
  buyable_now 排序。强制重发用 `ASTOCK_FORCE_PUSH=1`（默认关）。

## 两套系统严禁混淆（2026-09-14 用户明确强调）
| | 本仓库 astock-system | 另一套 stock-analysis |
|---|---|---|
| GitHub | **aprildream24/astock-system**（Pages 已开） | fisk9r/stock-analysis（CF Pages + 腾讯 SCF） |
| 本地路径 | `ZCodeProject\astock-system` | `WorkBuddy\2026-08-04-11-06-17\stock-analysis` |
| 主通道 | WxPusher（PushPlus/ServerChan 备用） | PushPlus / ServerChan |
| 账本 | 本地 `dist/push_ledger.json`（json） | 云端 COS `state/push_ledger.jsonl` |
- 本地自动化里 3 条 A 股任务（盘前兜底/竞价/盘中）的 cwds **指向另一套目录**且已 PAUSED，
  排查时先确认"这条任务到底服务哪套"。用户问"没收到推送"时**先问清是哪套**，不要串。

## astock-system 调度要点（2026-09-14）
- CI 门禁：`python tests/run_regression.py` 一挂，**后面所有构建+推送步骤全 skipped**
  ⇒ 回归失败 = 全天零推送。这是该系统"静默不推送"的第一嫌疑点。
- `stock.yml` 曾踩坑：`$GITHUB_SCHEDULE` 不是合法 Actions 变量（恒空），
  必须 `env: GH_SCHEDULE: ${{ github.event.schedule }}` 再 case。
- **CI 推送哑火双坑（2026-09-14 修）**：CI 无本地 notify.json 时
  ① `push_dry_run` 默认 True→配了 Secret 也只写账本不真发；
  ② `primary_channel` 默认 wxpusher→没配 WxPusher 账户时 PushPlus 分支永远不进。
  修在 `core.load_config()`：无 notify.json 时有 key 即真发 + 主通道跟随实际 key。
- **CI runner 无状态**：cache/market.db 不持久化 → 每次冷库全量重拉 20 分钟超时被杀。
  修法：`actions/cache@v4` 持久化 cache/（timeout 同时 20→45min）。
- **测试禁网络**：test_absorb 的 cross_check 用例曾依赖"本地恰好断网"，
  CI 能出网→境外 runner 访问新浪/腾讯超时→挂。已改确定性 mock 三源 return None。
  规则：**回归用例一律 mock 网络，不得依赖环境网络状态**。
- GitHub 自带 cron 在本仓库 **极不可靠**（延迟 1-2h+）——2026-09-14 晚实证它
  造成 build_close 重复推送后，**stock.yml 6 cron + executor.yml 16 cron 全删**，
  两个 workflow 只留 workflow_dispatch。**唯一权威触发 = cron-job.org 四定时器**
  （astock-pre 08:50 / auction 09:25 / close 15:22 / review 20:02，dispatches API
  带 task inputs，key 存 Temp/astock_cronjob_key.txt）。模拟盘巡逻若要恢复，
  需在 cron-job.org 补 dispatch 定时器（限流 13s）。
- **日级保险丝（不可回退）**：notifier.push() 内 `_daily_sent`——同 mode+同日期
  已 sent（state 账本 + dist 镜像双查）→ 拦截，force 可绕过。触发端怎么重复，
  收盘/盘前/叙事一天最多一条。failed/uncertain 放行可补发。
- **site 任务陷阱**：dispatch task=site 时"构建+推送"步骤必须早退（users.json
  由「构建加密站点」步骤写）——test_p8 已锁断言"站点任务不在本步骤推送"。
- GitHub key 历史遗留：ghp_BtAo...（2026-09-14 用户提供，存 Temp/astock_gh_token.txt，
  用于 gh_sync + Secrets 配置 + dispatch 验证）。

## astock-system 外部定时器（2026-09-14 起，权威触发器）
- cron-job.org 注册 4 个 astock-* 任务（pre 08:50 / auction 09:25 / close 15:22 /
  review 20:02，周一至五），直打 workflow_dispatch；GitHub 自带 cron 降为冗余。
- 账号同属于用户，云端共 28 任务：exec-*/stock-* 24 个 = 另一套 stock-analysis，
  **严禁动它们**；操作只限 astock- 前缀。key 落盘 Temp/astock_cronjob_key.txt。
- 接口：创建=PUT /jobs（POST 404）、schedule 数组结构、requestMethod 1=POST、
  创建限流 13s 间隔、更新不支持 PUT /jobs/{id}（先删后建，只删自己的）。
- dist/push_ledger.json 已入 CI cache → 跨 run 去重生效，双触发不重复推。

## 数据口径
- 全市场快照 ~5558 只，按 `mktfilter.tradable`（沪深主板+创业板）过滤后 ~4936 只。
- `cache/market.db`；新口径下有效标的 4592 只全部有当日K线（**覆盖 100%**）。
  344 只剔除=退市 340 + 未上市 4，已剥离分母并留痕，无需再跑 fetch_all 补数。

## 云端自选管理（2026-09-15 新增交付）
用户诉求：「我能够在网络上单独添加自选」「不要命令行要可视化」「特定用户特定访问」。

**架构决策（关键）**：站点是 GitHub Pages 纯静态，写不了文件；且 `config/watch.json`
在 `.gitignore`（自选/持仓名单是隐私红线，绝不入公开仓库）。三条路径比对后选定：
① 提交 watch.json 回仓库 —— ❌ 破隐私红线
② **更新仓库 Secret `WATCH_CODES`** —— ✅ 不公开 + CI 已在读（`_codes_conf` 合并 env）
③ 外部 KV —— 运维面变大

**实现**：`pipeline/cloud_watch.py`（本地/内网 HTTP 服务，端口 8771）
- 免命令行入口：**双击 `tools/云端自选面板.bat`**（自动读 `%TEMP%\astock_gh_token.txt`）
- 能力：代码规范化（`600519`/`sh600519`/`SH600519` → `sh600519`）、本地原子写、
  libsodium sealed box 加密写 Secret、深色 HUD 面板、增删后**自动同步云端**
- 安全：`X-Auth-Token` 门禁（`--host` 非 127.0.0.1 时**强制要求**，否则拒绝启动）；
  PAT 只在服务端、绝不下发到浏览器
- **真实云端验证通过**：`write_watch_secret` 实测写入成功（Secret updated_at 变动）
- 回归：`tests/test_cloud_watch.py` **26 tests OK**（含 sealed box 真加解密对拍）

⚠️ 分用户分级权限（`all`/`watch`/`observe`/`buy`）在**密文层**剥离，非前端隐藏。
实测 owner(watch_advice=4) vs guest(已剥离)；两个 .bin **文件大小可能相同**
（加密后 pad 到同块），**不能拿文件大小判断权限是否生效** —— 要解出载荷比。

## 实盘级体检（用户核心诉求：不要只报"修好了"）
用户原话：「每天都告诉我没有问题，结果一到实盘就是这样那样的问题」。
⇒ 交付 `dist/reports/e2e_体检报告_YYYYMMDD.html` 这种**基于真实端点探测**的报告，
而不是单元测试自证。区分三态：已修(实测) / 待验证 / 阻塞。

