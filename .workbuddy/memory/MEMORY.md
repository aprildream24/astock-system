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

## 回归纪律
- 入口 `python tests/run_regression.py`（cwd 自动切到 tests）。ALL_PASS 才过。
- 规则：PASS 数只许涨不许跌，FAIL 必须 0。基线写在 `tests/baseline.json`。
- 2026-09-14 基线：**PASS=137 FAIL=0**（09-13 两轮整改 89→109→131，09-14 智谱接入+真验 137）。

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

## 三条不可回退的红线（2026-09-13 整改固化）
1. **买区必须是可下单的窄带**。`entry_plan.now_zone` 宽度 ≤4.5%
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
- GitHub 自带 cron 在本仓库 **极不可靠**（建仓 2 天、8 个定时点只触发 1 次且延迟 ~2h）。
  本机兜底目前 **未注册**（AStocker-* 无），需管理员跑 `tools/install_schedule.bat`
  （已含 08:55 盘前 / 09:27 竞价 / 15:40 收盘 / 20:10 复盘 + 守护，绝对路径 python）。
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
