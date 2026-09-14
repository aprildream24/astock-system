# Astra 收盘观察系统（A股量化研究与行情观察工具）

纯 Python 标准库实现（零第三方依赖，Python 3.10+）。

**定位（M40）**：后台充分分析，前台少量、明确、可解释。数据不可靠就暂停，
条件未满足就等待，没有机会就不凑数，没有成交就不写成交。
本系统用于量化研究与模拟分析，不代理实盘、不保证收益；规则评分不是上涨概率。

**单通道架构（用户拍板 2026-09-13）**：调度与部署**只用 GitHub**
（Actions 调度 + GitHub Pages 发布）；推送主通道 **WxPusher 多账户**。

流程：行情采集（东财/腾讯/新浪三源容灾）→ 数据校验 → 市场环境识别（十维情绪）
→ 策略候选生成（只收当下可下单买入的标的）→ 统一决策与风控
→ 观察计划 / 模拟委托 → 推送与复盘归因。

> **密钥安全**：仓库源码内零密钥。所有密钥走 `config/*.json`（已 gitignore）
> 或 GitHub Secrets，模板全部备好。

---

## 三步上线（填 key 即用）

### 第 1 步：本地填 3 个配置

```bash
copy config\notify.example.json   config\notify.json     & rem WxPusher 多账户
copy config\users.example.json    config\users.json      & rem 站点口令
copy config\watch.example.json    config\watch.json      & rem 自选股（每日给建议）
copy config\holdings.example.json config\holdings.json   & rem 持仓（可选）
```

**WxPusher 多账户**（wxpusher.zjiecode.com 创建应用拿 APP_TOKEN，订阅者页拿 UID）：

```json
{
  "primary_channel": "wxpusher",
  "wxpusher_accounts": [
    {"name": "主号", "app_token": "AT_xxx", "uids": ["UID_xxx"]},
    {"name": "家人", "app_token": "AT_yyy", "uids": ["UID_yyy"]}
  ],
  "wxpusher_routes": {"*": ["主号", "家人"]}
}
```

- 加几个账户就发几个，**由你决定**；`wxpusher_routes` 控制谁收什么
  （键匹配：精确 mode → 前缀 `build`/`watch`/`exec` → `*`；缺省发全部）
- CI 部署时不落盘：仓库 Secret `WXPUSHER_CONF` 填账户数组的 JSON 字符串

**已有 PushPlus？主通道一行切换**（`primary_channel`：wxpusher | pushplus | serverchan）：

```json
{
  "primary_channel": "pushplus",
  "pushplus_token": "你的token"
}
```

**防混淆标识（push_tag）**：每条推送标题自动加 `【Astra·来源】` 前缀
（如 `【Astra·主号】收盘观察 09-11`、`【Astra·PushPlus】自选股操作建议`），
正文顶部有同源角标——多账户/多渠道混收一眼可辨，改 `push_tag` 即改标识。

**WxPusher 收不到消息的排查**：① UID 必须先关注其公众号并在后台订阅者页绑定；
② appToken 与应用对应；③ 免费档有日额度；④ 先在 WxPusher 后台发测试消息确认
通道本身通，再接系统。ServerChan/PushPlus 为可选备用通道（全部 WxPusher
账户明确失败才启用）。

### 第 2 步：本地自检 + 全流程

```bash
py tools\setup_check.py                      # 一键体检
py -X utf8 -m pipeline.fetch_daily           # 交易日收盘后：全市场增量更新
py tools\coverage_audit.py                   # 覆盖率体检：是否真的扫全了
py tools\push_preview.py                     # 先看版面再发（HTML+纯文本并排预览）
py -X utf8 -m pipeline.build --task close    # 收盘构建+推送（含可买分组+自选建议）
py -X utf8 -m pipeline.build --task site     # 加密站点
py -X utf8 -m tools.executor --task scan     # 模拟盘巡逻
```

> **改推送版面必先看预览**：`tools/push_preview.py` 用真实 K 线渲染一份推送
> 到 `dist/reports/push_layout_preview.html`（左＝微信/PushPlus webview，
> 右＝ServerChan 纯文本降级）。推送撤不回来，别靠脑补验收。

数据源容灾：东财/腾讯被限流时自动切换，新浪日K兜底
（2026-09-13 实测：东财+腾讯双封时新浪通道独立扛完全市场补齐）；
增量同步——已同步的票只补近端尾巴，全库更新约 2 分钟。

### 第 3 步：GitHub（调度 + Pages 唯一部署通道）

```bash
git init && git add -A
git grep -lE "(AT_[A-Za-z0-9]{8,}|SCT[A-Za-z0-9]{8,})" && echo "发现密钥，禁止提交！" || echo "零密钥 OK"
git commit -m "init" && git remote add origin <你的仓库URL> && git push -u origin master
```

仓库 Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。
Secrets 配置：

| Secret | 内容 | 必填 |
|---|---|---|
| `WXPUSHER_CONF` | `[{"name":"主号","app_token":"AT_xxx","uids":["UID_xxx"]},...]` | WxPusher 主通道时 |
| `PUSHPLUS_TOKEN` | PushPlus token（配合 primary_channel=pushplus） | PushPlus 主通道时 |
| `SITE_USERS` | `{"owner":"你的口令","guest":"访客口令"}` | 站点必填 |
| `SERVERCHAN_KEY` | 备用通道 | 可选 |

推送后 `stock.yml`（主链 9 时点，收盘构建自动发布 GitHub Pages）与
`executor.yml`（模拟盘 16 时点 + watchdog 缺跑守护）自动生效。
站点地址：`https://<用户名>.github.io/<仓库名>/`。

---

## 可买性口径（用户拍板 2026-09-13）

**主推荐只放「当下就能下单买入」的标的**。判断只有一句话的入口：
`scoring.is_buyable_now(c)` —— 六重闸门全过才算可下单，渲染层禁止自己重判：

| # | 闸门 | 不过的后果 |
|---|---|---|
| 1 | 市场准入（沪深主板/创业板） | 科创/北交/ETF/B股买不了 → 不推 |
| 2 | 未闯熔断（observe / broken） | 胜率不达标或已破位 → 不推 |
| 3 | 非当日涨停 | 封死买不进 → 归次日竞价通道 |
| 4 | 引擎四态 = 现在买 | 等回踩/小仓试/观望 都不是可执行 |
| 5 | 买区自洽（窄带≤8%/不倒挂/有盈利空间） | 伪区间推了也下不了单 |
| 6 | 现价确实落在买区内（dist_pct==0） | 跳出买区 → 进「待回踩·勿按现价追」 |

其余可买性口径：

- 当日已涨停（一字/封死）→ 买不进 → 归「🎯次日竞价确认」独立分组（最多2只），
  推送与网页都不与"现在可买"混排；低开直接放弃（历史胜率仅24%）
- 停牌/零成交 → 剔除并记录原因
- 科创板/北交所/ETF/B股 → 市场准入白名单直接不推（"推出去的票=能买的票"）
- ST/退市/次新 → 名称过滤
- 急跌票（当日 ≤-5%）→ 自选建议标"急跌不接刀"，不当日喊买

## 自选股每日操作建议

`config/watch.json` 加入代码（如 `"sh600359"`），每天收盘构建自动生成：

- **未持仓语境**（规格书 十）：可买（回落至买区）/ 微超 X% / 等回踩（挂单等回落至 Y）
  / 过热（追高风险）/ 已破位（移出自选）/ 急跌（不接刀）/ 已涨停（次日通道）
  ——禁止出现"持有"字样
- **持仓语境**：止损/持有/减仓裁决
- 独立推送一条「自选股操作建议」（不与主报告互相吃去重）+ 网页概览卡片

## 目录结构

```
pipeline/
  core.py         SQLite / 交易日历 / 防封禁三件套 / 双通道并发抓取 / 日志脱敏
  fetch_daily.py  全市场分页快照 + 增量同步 + 法定节假日守门 + 缺口自愈
  fetch_all.py    (tools) 分块续传抓取器：封禁探测等待/断点续传
  multi_source.py 三源交叉验证（东财/新浪/腾讯中位数权威价）
  quality.py      成交额分级守门 / 量纲修复（真实股本锚）/ 覆盖率分级
  emotion.py      十维情绪温度计 / 周期定位
  engines.py      趋势/箱体波段/快箱体/连板空间计划/回马枪/Kronos/近端买点
  scoring.py      环境加权唯一入口 / 统一评分 / 胜率熔断闸 / TOP3 终审
  decisions.py    统一决策对象 N04 / 信号生命周期 / 评级与执行分离
  watchlist.py    自选股每日操作建议（未持仓语境翻译）
  recveto.py      败因否决器 / 竞价低开闸
  risklevel.py    持仓红黄蓝三级灯
  alerts.py       触发式盯盘（止损/止盈/买点/锁定）
  recperf.py      推荐池胜率曲线（附录B披露口径）
  datacenter.py   两融/ETF资金流/龙虎榜/大宗/题材小引擎群
  executor.py     RiskGate / 批次T+1 / 订单成交分账 / 止损规则优先级
  wxpusher.py     WxPusher 多账户发送器（路由/三态受理）
  notifier.py     变化式主推送 / 三态账本 / 主备通道 / 昨日推荐复核
  publish.py      M38 认证加密 / owner 字段裁剪 / 部署红线
  build.py        编排入口（四池扫描/可买分组/自选建议/结局回填/竞价裁决）
tools/            check_strategy_lock / setup_check / verify_site / watchdog
                  fetch_all / coverage_audit / push_preview / install_schedule.bat
docs/STRATEGY_LOCK.md  策略锁清单（M20 可审计）
.github/workflows/     stock.yml（9时点+Pages发布）/ executor.yml（16时点+守护）
site_template/   零依赖前端（WebCrypto 认证加密 + 仪表盘 4 视图）
tests/           回归测试 10 套件（PASS=131 基线）
```

## 关键纪律（改动必读）

1. 回归底线：`py tests\run_regression.py`，PASS 只许涨不许跌；
2. 胜率熔断 observe 全通道剔除；技巧只增不减（基线守门）；
3. T+1 按批次管理；风险触发≠成交（跌停卖不出记录留痕）；
4. 推送三态账本：受理不确定不盲目双发；重要风险 force 绕过去重；
5. AI 只解释不决策；PBKDF2+HMAC 认证加密，口令泄露需轮换；
6. 引擎阈值有实证出处，调参需等量级回测证据。

## 历史统计披露格式（附录B）

```
策略名称/规则版本/样本时间/样本数量/股票池口径/信号可用时间/入场价格口径/
退出规则/费用与滑点/不可成交处理/样本内外/胜率/平均收益/最大回撤/局限说明
```

## 已知边界（如实声明）

- 54 技巧中 37 个 planned（妖股/缠论/退潮等需逐个按规格书移植）；
- 炸板/涨停判定为日K近似口径；Actions 环境每日增量抓取；
- 覆盖率分母只算「当日有成交的可交易标的」：名单源陈旧，含 340 只已退市老代码
  与 4 只未上市新股（2026-09-13 实测），它们扫不到也买不了，单独留痕不计缺口；
- 模拟盘为简化撮合；AI 叙事为可选单模型（未配 key 时规则引擎兜底）；
- 竞价强时效任务在 Actions 上有分钟级延迟（N09：实测后决定是否迁移）。
