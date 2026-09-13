# 策略锁（STRATEGY LOCK）——只能更新，不能去除（M20 修订版）

> 本清单枚举已固化的策略函数。任何改动允许**优化参数/逻辑**（更新），但
> **删除函数、改函数名、移除调用点**都视为违规。`tools/check_strategy_lock.py`
> 以 `baseline_techniques.json` 强制校验（技巧数量只许涨不许跌）；
> `pipeline/build.py` 每次构建前自动执行同一守门。

> **M20 语义**：从「技巧只增不减」升级为「策略变更可审计、风控变更可回滚」
> ——保留防止情绪化拆风控的初衷，同时允许依据充分证据修正或移除无效规则。
> 下线一条锁定规则的唯一合法路径：①实证依据写入本文件变更记录；②用户明确
> 同意；③同步下调 baseline_techniques.json（git 全程可回溯）。

## 锁定清单（pipeline/）

### 数据底座
| 模块.函数 | 作用 | 来源 |
|---|---|---|
| `core.RateLimiter` / `SourceGuard` / `kline_batch` | 防封禁三件套 | 规格 #601-A |
| `core.is_trading_day_cross` / `purge_fake_days` | 交易日历交叉确认 / 清洗先隔离 | 审计 M04 |
| `core.fetch_open_snapshot` | 未开盘快照过滤 | #605-② |
| `quality.grade_total_amount` / `repair_volume_units` | 成交额分级守门 / 量纲修复 | 审计 M02/M03 |
| `gapfill.self_heal` | K线缺口自愈（今日半根删除重补） | 原项目 backfill/repair_gap |
| `multi_source.cross_check` | 三源交叉验证（中位数为权威价） | 原项目 multi_source，M01 |
| `trade_calendar.is_trade_day` | 法定节假日日历 | 原项目 trade_calendar |
| `mktfilter.tradable` | 市场准入（沪深主板+创业板） | 原项目 mktfilter #486 |

### 分析引擎
| 模块.函数 | 作用 |
|---|---|
| `engines.screen_uptrend` | 趋势双通道（主通道/缓坡通道） |
| `engines.detect_stage_bottom` | 箱体波段（破位0.99/双闸/30日阴跌闸） |
| `engines.classify_box_speed` | 快箱体三要素（最深回踩=min(c15[hi_idx:])，勿改） |
| `engines.ladderplan_plan` / `auction_discipline` | 连板空间计划 / 竞价纪律（st=2 需≥5%） |
| `engines.screen_pullback_relay` | 涨停回马枪 |
| `engines.kronos_lite` / `entry_plan` | Kronos 结构健康度 / 近端买点四态 |
| `recveto.veto` / `auction_gate` | 败因否决器（标注式）+ 竞价低开闸 |
| `emotion.emotion_ten` / `anchor_score` | 十维情绪温度计 / 锚点插值 |
| `mood.compute_mood` | 涨停池统计（晋级率/炸板率，环境层同源） |

### 决策与推送
| 模块.函数 | 作用 |
|---|---|
| `scoring.env_weights` | 环境加权唯一入口（M09/M10） |
| `scoring.tag_winrate` / `observe_mute` / `compute_top_picks` | 胜率熔断闸 / TOP3 终审 |
| `decisions.make_decision` / `advance_signals` | 统一决策对象 N04 / 信号生命周期 |
| `notifier._cand_line` / `render_brief` / `_prev_pick_status` | 候选行单一出口 / 变化式主报告 / 昨日复核 |
| `notifier.push` | 三态受理账本（M37）+ 主备通道（M36） |

### 模拟盘
| 模块.函数 | 作用 |
|---|---|
| `executor.place_order` / `evaluate_exit` / `available_qty` | N06 下单前检查 / 止损规则优先级 M24 / 批次 T+1 M26 |
| `executor.ensure_account` / `equity` | 日切与熔断复位 M22 / 净值对账 M32 |
| `risklevel.compute` | 持仓红黄蓝三级灯 |
| `alerts.build_triggers` | 触发式盯盘（止损/止盈/买点/锁定） |

### 规则
1. 新增固化策略时，同步更新本文件、`pipeline/techniques.py` 注册表，并
   `python tools/check_strategy_lock.py --update` 调高基线。
2. 改动后必须跑：`py tests\run_regression.py`（PASS 只许涨不许跌）。
3. 涉及推送的改动，必须本地干跑验证渲染链路。
