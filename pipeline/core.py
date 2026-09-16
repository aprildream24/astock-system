# -*- coding: utf-8 -*-
"""数据层核心：SQLite 建库 / 交易日历守门 / 防封禁三件套 / 数据质量防线。

纯 Python 标准库实现（Python 3.10+），零第三方依赖。
所有密钥一律通过 config/notify.json 或环境变量注入，源码内只有占位符。
"""
import json
import os
import random
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
DIST_DIR = os.path.join(BASE_DIR, "dist")
SITE_DIR = os.path.join(BASE_DIR, "site")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DB_PATH = os.path.join(CACHE_DIR, "market.db")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 内网代理劫持时强制直连（规格 2.1）
try:
    import ssl  # noqa: F401
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urllib.request.install_opener(_opener)
except Exception:
    pass


# ---------------------------------------------------------------------------
# 配置加载：真实密钥只存在于 config/notify.json（已 gitignore）或环境变量
# ---------------------------------------------------------------------------

def load_config():
    """读取 config/notify.json；不存在则返回全占位符，保证零密钥可运行。"""
    cfg = {
        "serverchan_key": os.environ.get("SERVERCHAN_KEY", ""),
        "pushplus_token": os.environ.get("PUSHPLUS_TOKEN", ""),
        "wxpusher_accounts": [],
        "wxpusher_routes": {},
        "primary_channel": "wxpusher",   # wxpusher | pushplus | serverchan
        "push_tag": "Astra",             # 防混淆标识：【{tag}·来源】标题前缀
        "site_url": "https://aprildream24.github.io/astock-system/",
        "site_password": os.environ.get("SITE_PASSWORD", ""),
        "admin_password": os.environ.get("ADMIN_PASSWORD", ""),
        "owner_id": os.environ.get("OWNER_ID", "owner"),
        "push_dry_run": True,  # 未配置任何 key 时只写账本不发送
    }
    path = os.path.join(CONFIG_DIR, "notify.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    if os.environ.get("WXPUSHER_CONF"):
        try:
            env_accounts = json.loads(os.environ["WXPUSHER_CONF"])
            if isinstance(env_accounts, list):
                by_name = {a.get("name"): a for a in cfg["wxpusher_accounts"]}
                for a in env_accounts:
                    if isinstance(a, dict) and a.get("name"):
                        by_name[a["name"]] = a
                cfg["wxpusher_accounts"] = list(by_name.values())
        except Exception:  # noqa: BLE001 — Secret 格式错误不阻断
            pass
    has_key = bool(cfg.get("serverchan_key") or cfg.get("pushplus_token")
                   or cfg.get("wxpusher_accounts")
                   or os.environ.get("WXPUSHER_CONF"))
    # 归一化 push_dry_run：容忍 "false"/"true" 字符串写法
    _dr = cfg.get("push_dry_run")
    if isinstance(_dr, str):
        cfg["push_dry_run"] = _dr.strip().lower() not in ("false", "0", "no", "")
    if not has_key:
        cfg["push_dry_run"] = True
    elif cfg.get("push_dry_run") and \
            not os.path.exists(os.path.join(CONFIG_DIR, "notify.json")):
        # CI 场景：无本地配置文件、Secrets 已注入 key ⇒ 真发。
        # （原来默认 dry-run 会把 CI 全部变哑火：账本写了、消息永远不出。）
        cfg["push_dry_run"] = False
    # CI 场景：主通道跟随实际配置的 key（原来恒默认 wxpusher，
    # 没配 WxPusher 账户时 PushPlus 分支永远不进 → 同样哑火）
    if not os.path.exists(os.path.join(CONFIG_DIR, "notify.json")):
        if not cfg.get("wxpusher_accounts") and cfg.get("pushplus_token"):
            cfg["primary_channel"] = "pushplus"
        elif not cfg.get("wxpusher_accounts") and cfg.get("serverchan_key"):
            cfg["primary_channel"] = "serverchan"
    return cfg


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS klines(
    code TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL,
    v REAL, amt REAL, pct REAL, turn REAL,
    PRIMARY KEY(code, date));
CREATE INDEX IF NOT EXISTS idx_klines_date ON klines(date);
CREATE TABLE IF NOT EXISTS zt_pool(
    date TEXT, code TEXT, streak INTEGER, name TEXT,
    PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS rec_picks(
    date TEXT, code TEXT, name TEXT, tag TEXT, action TEXT,
    buy_low REAL, buy_high REAL, stop REAL, sell_low REAL, sell_high REAL,
    score REAL, outcome TEXT DEFAULT '', outcome_ret REAL,
    PRIMARY KEY(date, code, tag));
CREATE TABLE IF NOT EXISTS signal_ledger(
    sid TEXT PRIMARY KEY, code TEXT, strategy TEXT, date TEXT,
    state TEXT, changed_at TEXT);
CREATE TABLE IF NOT EXISTS candidate_snapshots(
    date TEXT, code TEXT, name TEXT, pool TEXT, score REAL, action TEXT,
    reason TEXT, extra TEXT, PRIMARY KEY(date, code, pool));
CREATE TABLE IF NOT EXISTS push_ledger(
    biz_key TEXT PRIMARY KEY, mode TEXT, ts TEXT, dist_ok INTEGER,
    status TEXT DEFAULT 'pending', channel TEXT DEFAULT '',
    detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS batch_meta(
    batch_id TEXT PRIMARY KEY, source TEXT, trade_date TEXT,
    source_time TEXT, fetched_at TEXT, field_caliber TEXT,
    quality TEXT, data_version TEXT, extra TEXT);
CREATE TABLE IF NOT EXISTS repair_log(
    ts TEXT, code TEXT, date TEXT, field TEXT, old_val REAL,
    new_val REAL, basis TEXT, rule_version TEXT);
CREATE TABLE IF NOT EXISTS signals(
    signal_id TEXT PRIMARY KEY, code TEXT, strategy TEXT,
    rule_version TEXT, created_at TEXT, data_date TEXT,
    status TEXT, zone_low REAL, zone_high REAL, stop REAL,
    invalid_if TEXT, valid_until TEXT, reason TEXT,
    status_reason TEXT DEFAULT '', changed_at TEXT);
CREATE TABLE IF NOT EXISTS emotion_log(
    date TEXT PRIMARY KEY, score REAL, effective INTEGER,
    coverage REAL, qualified INTEGER, phase TEXT, parts TEXT);
CREATE TABLE IF NOT EXISTS position_batches(
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT,
    buy_date TEXT, qty REAL, cost REAL, available REAL, strategy TEXT);
CREATE TABLE IF NOT EXISTS orders(
    order_id TEXT PRIMARY KEY, ts TEXT, code TEXT, side TEXT,
    qty REAL, price REAL, status TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS fills(
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts TEXT,
    code TEXT, side TEXT, qty REAL, price REAL, fee REAL);
CREATE TABLE IF NOT EXISTS cashflow(
    ts TEXT, type TEXT, amount REAL, balance REAL, note TEXT);
CREATE TABLE IF NOT EXISTS account_state(
    id INTEGER PRIMARY KEY CHECK(id=1), cash REAL,
    day_start_equity REAL, day_key TEXT, frozen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshot(
    date TEXT, code TEXT, name TEXT, price REAL, pct REAL,
    amt REAL, turn REAL, fmv REAL,
    PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS idx_snapshot_date ON snapshot(date);
CREATE TABLE IF NOT EXISTS holdings(
    code TEXT PRIMARY KEY, name TEXT, buy_date TEXT, buy_price REAL,
    shares REAL, stop REAL);
-- 2026-09-16 新增：盘中实时快照（M41）。**与 snapshot 主表物理隔离**——
-- 盘中价不是收盘价，混进主表会让次日全部引擎基于假收盘价出信号
-- （09-16「候选 0」血案同源风险）。本表只服务「盘中计划校验」，
-- 需人工显式查询，不进任何引擎计算链路。
CREATE TABLE IF NOT EXISTS snapshot_live(
    date TEXT, slot TEXT, code TEXT, name TEXT,
    price REAL, pct REAL, amt REAL,
    PRIMARY KEY(date, slot, code));
CREATE TABLE IF NOT EXISTS exec_log(
    ts TEXT, code TEXT, action TEXT, price REAL, reason TEXT);
"""


def get_conn(path=DB_PATH):
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(_SCHEMA)
    return con


# ---------------------------------------------------------------------------
# 交易日历守门（防线1：非交易日拒绝写库）
# ---------------------------------------------------------------------------

def trade_calendar(con):
    """以 sh000001 日K 为权威日历，返回全部交易日列表（升序）。"""
    rows = con.execute(
        "SELECT DISTINCT date FROM klines WHERE code='sh000001' ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows]


def is_real_trade_day(date):
    """该日期**本身**是否为沪深交易日（用权威节假日日历判断）。

    ⚠️ 2026-09-15 循环依赖修复：`trade_calendar(con)` 由 `klines` 表推导，
    **当日指数K线入库前它必然不含当天**。而 `is_trading_day_cross` /
    `data_ready_for` 第一关都是 `date in trade_calendar(con)` ⇒
    当天数据尚未抓到时，"今天"被判成「非交易日」→ `close` 拒绝构建
    →（rc=0，静默）→ 用户零推送。
    这是"闸门依赖被闸门管控的数据"的循环依赖：
    抓取超时 ⇒ 指数K线不入库 ⇒ 闸门永远判非交易日。

    所以判断"是不是交易日"必须回退到**独立于本地数据的权威日历**
    （`trade_calendar.is_trade_day`，源自国务院放假安排），
    而不是"我的库里有没有这根K线"。
    """
    from .trade_calendar import is_trade_day
    try:
        return bool(is_trade_day(date))
    except Exception:  # noqa: BLE001 —— 日历异常时保守放行，绝不因日历故障漏推
        return True


def is_trading_day(con, date):
    """本地库里是否已有该交易日的指数K线（数据口径，非日历口径）。

    语义澄清：这是"数据到位"判断。要问"这天客观上是否开市"，
    用 `is_real_trade_day(date)`。
    """
    return date in trade_calendar(con)


def is_trading_day_cross(con, date):
    """M04：交易日历交叉确认——单一指数日K不是唯一权威。

    判定顺序（2026-09-15 修复循环依赖后）：
      ① **权威日历**说这天是交易日吗？不是 → 直接拒（无需本地数据）；
      ② 本地已有该日快照？有 → 看 pct 是否全零（全零=疑似休市）；
      ③ 本地**暂无**该日数据 → 不代表休市，只代表**还没抓**。
         此时若权威日历说是交易日，返回"待抓取"，由调用方决定是否等待，
         **不得直接判成"非交易日"**（那正是零推送的成因）。
    返回 (certain: bool, reason)。
    """
    if not is_real_trade_day(date):
        return False, "权威日历：非交易日（法定休市/周末）"
    row = con.execute(
        "SELECT COUNT(*), SUM(CASE WHEN ABS(COALESCE(pct,0))>0.0001 THEN 1 "
        "ELSE 0 END) FROM snapshot WHERE date=?", (date,)).fetchone()
    total, nonzero = row[0], row[1] or 0
    if total:
        if nonzero == 0:
            return False, "快照 pct 全零：疑似休市日"
        return True, "权威日历 + 当日快照 pct 交叉确认"
    # 权威日历说是交易日，但本地还没数据 —— 这是"待数据"，不是"非交易日"
    return False, f"{date} 是交易日但本地尚无快照（待抓取入库）"


def redact(text, *secrets):
    """N11：日志脱敏——避免 SendKey/token 出现在错误信息或日志里。"""
    if not text:
        return text
    for s in secrets:
        if s:
            text = str(text).replace(str(s), "***")
    # URL 查询参数里的常见凭据字段
    return re.sub(r"([?&](?:token|key|sendkey)=)[^&\s]+", r"\1***",
                  str(text), flags=re.I)


def purge_fake_days(con, dates):
    """M04：异常日期清洗——先隔离备份（quality.quarantine_records），
    审计后执行删除；日志/告警/推送/配置表不受非交易日守门限制。"""
    from . import quality
    try:
        quality.quarantine_records(con, dates)
    except Exception:  # noqa: BLE001 — 备份失败不阻断清理，但要留痕
        print("[purge] WARN: 隔离备份失败，继续清理")
    for d in dates:
        for tbl in ("klines", "zt_pool", "rec_picks", "candidate_snapshots",
                    "snapshot"):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})")]
            if "date" in cols:
                con.execute(f"DELETE FROM {tbl} WHERE date=?", (d,))
    con.commit()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def prev_trading_day(con, date):
    days = trade_calendar(con)
    prevs = [d for d in days if d < date]
    return prevs[-1] if prevs else None


# ---------------------------------------------------------------------------
# #601-A 防封禁三件套
# ---------------------------------------------------------------------------

class RateLimiter:
    """① 自适应令牌桶：按 host 管「平时跑多快」。"""

    def __init__(self, rate=10.0, lo=3.0, hi=20.0):
        self.rate = rate
        self.lo, self.hi = lo, hi
        self._ok_streak = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            wait = 1.0 / self.rate - (time.time() - self._last)
            if wait > 0:
                time.sleep(min(wait, 0.5))
            self._last = time.time()

    def note_throttled(self):
        with self._lock:
            self.rate = max(self.lo, self.rate * 0.7)
            self._ok_streak = 0

    def note_ok(self):
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= 25:
                self.rate = min(self.hi, self.rate * 1.25 + 0.3)
                self._ok_streak = 0


class BanBlocked(Exception):
    """熔断冷却期内：请求进门即拒，不发网络包。"""


class SourceGuard:
    """② 封禁熔断器：按 host 管「被封后何时停手」。"""

    def __init__(self, fails_to_trip=4, cooldown=45.0, backoff=2.0,
                 max_cooldown=3600.0):
        self.fails_to_trip = fails_to_trip
        self.cooldown0 = cooldown
        self.backoff = backoff
        self.max_cooldown = max_cooldown
        self.fails = 0
        self.trips = 0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def blocked(self):
        return time.time() < self._blocked_until

    def note_fail(self):
        with self._lock:
            self.fails += 1
            if self.fails >= self.fails_to_trip:
                self.trips += 1
                cd = min(self.max_cooldown,
                         self.cooldown0 * (self.backoff ** (self.trips - 1)))
                self._blocked_until = time.time() + cd
                self.fails = 0

    def note_ok(self):
        with self._lock:
            self.fails = 0
            self.trips = 0
            self._blocked_until = 0.0

    def probe_ready(self):
        """冷却期满且熔断过 → 允许单次半开探测。"""
        return self.trips > 0 and not self.blocked()

    def note_probe(self, ok):
        if ok:
            self.note_ok()
        else:
            with self._lock:
                self.trips += 1
                cd = min(self.max_cooldown,
                         self.cooldown0 * (self.backoff ** (self.trips - 1)))
                self._blocked_until = time.time() + cd

    def health(self):
        return {"blocked": self.blocked(), "fails": self.fails,
                "trips": self.trips}


_LIMITERS = {}
_GUARDS = {}
_M = threading.Lock()

EM_HOST = "push2his.eastmoney.com"
# 裸域名 ifzq 与 web.ifzq 是独立 WAF 策略，且**会各自单独失效**：
#   2026-09-13 实测 web.ifzq 被封、裸域名 ifzq 可用 → 当时选了裸域名；
#   2026-09-14/15 反转：裸域名 ifzq 全面返回 **HTTP 501**（端点已下线），
#   web.ifzq 恢复可用。而 501 不在熔断码（403/429/418）里 → 裸域名
#   **永远不会被熔断**，每只股票都要白等 6–15 秒重试退避，
#   全市场 4900 只 ⇒ 抓取被拖爆 timeout，第 8 步「构建+推送」整步 skipped
#   ⇒ 用户全天零推送。这是"每天说没问题、实盘就出问题"的真凶。
# 结论：**不能写死单个域名**。TX_HOSTS 按顺序尝试，谁先给出有效行就用谁，
# 且把 501/404 一并计入 guard 失败（端点级失效必须能被熔断）。
TX_HOSTS = ("web.ifzq.gtimg.cn", "ifzq.gtimg.cn")
TX_HOST = TX_HOSTS[0]        # 兼容旧引用；实际请求走 TX_HOSTS 轮转
# 端点级失效码：501 未实现 / 404 不存在 / 410 已下线 —— 必须能让 guard 熔断，
# 否则会像裸域名 ifzq 那样「每次请求都白等重试」，把整个抓取拖死。
DEAD_CODES = (404, 410, 501)

# ---------------------------------------------------------------------------
# 限速档位（2026-09-15 实测标定，**这是全市场抓取耗时的唯一决定因素**）
# ---------------------------------------------------------------------------
# 背景：RateLimiter 默认 rate=10 req/s，且按 host 全局共享。当日 em/tx 双死、
# 只剩 sina 独活时，12~24 并发也被这个令牌桶压回 10 req/s ⇒ 4937 只理论下限
# 494s，实测叠加往返/自适应抖动漂到 16~19 分钟，再叠任何重试就冲破超时
#（E2E 里 fetch 被掐在 3000s；CI 里 45min 步超时同样吃紧）。
#
# 实测（quotes.sina.cn，单请求 ~0.47s）：
#   rate=10 w=12 → 0.196s/票 → 全市场 16.1min
#   rate=20 w=20 → 0.169s/票 → 全市场 13.9min
# 且全程 ok=240/240，无封禁迹象。sina 是**独立 CDN、与腾讯/东财互不影响**，
# 故给它单独的高速率档；EM/TX 维持保守值（它们随时可能复活，复活后
# 双通道并行，每 host 10/s 的保守值反而是正确的）。
RATE_PROFILES = {
    # host: (rate, lo, hi)
    "quotes.sina.cn":                (20.0, 8.0, 26.0),
    "money.finance.sina.com.cn":     ( 6.0, 2.0,  8.0),  # 该域名易 456，必须保守
    "hq.sinajs.cn":                  ( 8.0, 3.0, 12.0),
    "push2his.eastmoney.com":        (10.0, 3.0, 20.0),
    "web.ifzq.gtimg.cn":             (10.0, 3.0, 20.0),
    "ifzq.gtimg.cn":                 (10.0, 3.0, 20.0),
    "qt.gtimg.cn":                   (10.0, 3.0, 20.0),
}
RATE_DEFAULT = (10.0, 3.0, 20.0)


def limiter(host):
    with _M:
        l = _LIMITERS.get(host)
        if l is None:
            l = RateLimiter(*RATE_PROFILES.get(host, RATE_DEFAULT))
            _LIMITERS[host] = l
        return l


def guard(host):
    with _M:
        return _GUARDS.setdefault(host, SourceGuard())


def guards_health():
    return {h: g.health() for h, g in _GUARDS.items()}


def fetch_text(url, timeout=10, retries=2, referer=None):
    """带浏览器 UA 的 GET；限流/失败计入 guard；BanBlocked 短路。
    referer：部分源（EM 数据中心）强制校验 Referer，缺失返回 403。

    2026-09-15 修复：**端点级失效（404/410/501）不重试**。
    裸域名 ifzq 返回 501 时，旧逻辑把它当普通失败，每票重试 2 次 + 指数退避，
    单票白等 6–15 秒；全市场 4900 只 ⇒ 抓取被拖爆 timeout ⇒ 全天零推送。
    现在这类码直接 note_fail 后抛出，交给上游换源，不浪费退避时间。
    """
    host = re.sub(r"https?://([^/]+).*", r"\1", url)
    g, lim = guard(host), limiter(host)
    if g.blocked():
        raise BanBlocked(host)
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    last_err = None
    for i in range(retries + 1):
        lim.acquire()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            lim.note_ok()
            g.note_ok()
            return data.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (403, 429, 418):
                lim.note_throttled()
                g.note_fail()
            elif e.code in DEAD_CODES:
                # 端点已下线：重试无意义，计入失败让 guard 熔断本 host
                g.note_fail()
                raise
            if e.code in (403, 429, 418) and g.blocked():
                raise BanBlocked(host)
        except Exception as e:  # noqa: BLE001
            last_err = e
            g.note_fail()
            if g.blocked():
                raise BanBlocked(host)
        # 重试退避：指数 ×±25% 抖动，防雪崩同步重试
        time.sleep(min(5.0, 0.5 * (2 ** i)) * (0.6 + random.random() * 0.8))
    raise last_err if last_err else RuntimeError(url)


def _kline_one_em(num, pfx, days):
    secid = ("1." if pfx == "sh" else "0.") + num
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=101&fqt=1&end=20500101&lmt={days}")
    js = json.loads(fetch_text(url))
    kl = (js.get("data") or {}).get("klines") or []
    return [[p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]),
             float(p[5])] for p in (r.split(",") for r in kl)]


def _kline_one_tx(num, pfx, days):
    """腾讯日K。域名会单独失效（见 TX_HOSTS 注释），逐个尝试直到拿到有效行。"""
    last_err = None
    for host in TX_HOSTS:
        url = (f"https://{host}/appstock/app/fqkline/get?"
               f"param={pfx}{num},day,,,{days},qfq")
        try:
            js = json.loads(fetch_text(url))
        except BanBlocked:
            last_err = BanBlocked(host)
            continue                     # 该域名被熔断/失效 → 换下一个
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        node = js.get("data", {}).get(f"{pfx}{num}", {})
        rows = node.get("qfqday") or node.get("day") or []
        if rows:
            return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                     float(r[5])] for r in rows if len(r) >= 6]
    if last_err:
        raise last_err
    return []


def _kline_one_sina(num, pfx, days):
    """新浪日K（第三独立源，规格书 2.1）。量纲为「股」→ 统一 ÷100 转「手」，
    与腾讯/东财口径一致（M03 量纲锚依赖统一单位）。"""
    limiter("quotes.sina.cn").acquire()
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/"
           f"CN_MarketDataService.getKLineData?symbol={pfx}{num}"
           f"&scale=240&ma=no&datalen={days}")
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA, "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        txt = resp.read().decode("utf-8", "replace")
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    out = []
    for d in data:
        try:
            out.append([d["day"], float(d["open"]), float(d["close"]),
                        float(d["high"]), float(d["low"]),
                        round(float(d["volume"]) / 100, 1)])
        except Exception:  # noqa: BLE001
            continue
    return out


def kline_batch(codes, days=40, con=None, workers=20):
    """③ 双源轮转 + 双通道并发（吸收原项目 backfill.py）。

    `days` 默认 40（2026-09-16 从 260 下调）：这是**安全上限**性质的默认值，
    防止遗漏传参的调用方静默拉一年历史。引擎/指标最大回看 32 根
    （publish.py 的 `[-32:]`），40 根留 +25% 余量。
    需要更长历史请显式传 days（如 tools/fetch_all.py 的 --days 120），
    或依赖库里**只增不改**的存量 K线——本参数不删旧数据。

    东财/腾讯交错各领一半，互不抢同一 host 令牌桶；任一通道主源
    熔断（BanBlocked）→ 本票直接改走对侧；对侧也熔断 → 放弃本票
    留待下轮（宁可缺数据也不打封禁源）。RateLimiter/SourceGuard
    线程安全，聚合速率仍受每 host 10 req/s 约束。

    2026-09-15：腾讯域名可轮转（TX_HOSTS），本函数只需确认
    **全部**候选域名都在熔断态才放弃该通道。

    2026-09-15 二次修复（严重）：**删掉「em+tx 全熔断就直接 return {}」的守卫**。
    该守卫把「主源双死」误判成「所有源都死」，可当日真实情况正是 em+tx 双死、
    新浪独活 —— 结果是函数 0.01s 返回空字典，连新浪兜底都不跑，全市场 0/4937。
    正确语义：**只有连兜底源（sina）都拿不到数据才叫失败**，绝不在入口提前放弃。
    """
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    out_lock = threading.Lock()

    # 2026-09-15：**死源短路**。em / tx 的日K端点当日已实测全线失效
    #   （em RemoteDisconnected、tx 两个域名均 HTTP 501），fail-fast 后
    #   guard 会很快进入 blocked 态。但原实现里每只票仍会「先撞 em、再撞 tx」，
    #   白白花掉 2~2.5s/票 —— 12 个并发槽全被死源吃掉，吞吐被拖到 1/50，
    #   全市场抓取从 4 分钟劣化成 46 分钟（恰好卡死 CI 的 45 分钟超时）。
    #   修法：在 pull 入口就检查熔断态，已 blocked 的源**不再发请求**，
    #   直接落到下一个候选源。注意这里只是「少发请求」，**不提前退出函数**。
    def _skip(host_or_hosts):
        if isinstance(host_or_hosts, str):
            return guard(host_or_hosts).blocked()
        return all(guard(h).blocked() for h in host_or_hosts)

    def pull(kind, num, pfx):
        rows = None
        if kind == "em":
            if not _skip(EM_HOST):
                try:
                    rows = _kline_one_em(num, pfx, days)
                except Exception:  # noqa: BLE001 — 含 BanBlocked，统一切对侧
                    rows = None
            if not rows and not _skip(TX_HOSTS):
                try:
                    rows = _kline_one_tx(num, pfx, days)
                except Exception:  # noqa: BLE001
                    rows = None
        else:
            if not _skip(TX_HOSTS):
                try:
                    rows = _kline_one_tx(num, pfx, days)
                except Exception:  # noqa: BLE001
                    rows = None
            if not rows and not _skip(EM_HOST):
                try:
                    rows = _kline_one_em(num, pfx, days)
                except Exception:  # noqa: BLE001
                    rows = None
        if not rows:
            try:
                rows = _kline_one_sina(num, pfx, days)   # 独立源兜底（当日唯一活源）
            except Exception:  # noqa: BLE001
                rows = None
        if rows:
            with out_lock:
                out[num] = rows

    def run_channel(kind, items, w):
        if not items:
            return
        with ThreadPoolExecutor(max_workers=w) as ex:
            list(ex.map(lambda it: pull(kind, it[0], it[1]), items))

    jobs = list(codes)          # 每项 = (num, pfx)
    # 2026-09-15：**通道数自适应**。原实现固定切两半、两通道各 workers 并发。
    # 但当某侧全熔断（当日 em+tx 双死）时，那一半线程池只会空转/串行等待
    # —— 票虽然最终会落到 sina，却仍占着该通道槽位，等于把有效并发砍半。
    # 现在：主源双死时直接用**单通道跑满 workers**，把所有槽位给唯一活源。
    em_dead = _skip(EM_HOST)
    tx_dead = _skip(TX_HOSTS)

    if em_dead and tx_dead:
        run_channel("single", jobs, workers)     # 唯一活源 = sina（pull 内兜底）
    elif tx_dead:
        run_channel("em", jobs, workers)
    elif em_dead:
        run_channel("tx", jobs, workers)
    else:
        th1 = threading.Thread(target=run_channel,
                               args=("em", jobs[0::2], workers))
        th2 = threading.Thread(target=run_channel,
                               args=("tx", jobs[1::2], workers))
        th1.start()
        th2.start()
        th1.join()
        th2.join()
    return out


def fetch_open_snapshot(codes):
    """腾讯 qt.gtimg 完整行情快照（f[3]=现价 f[4]=昨收 f[5]=今开），GBK。

    #605-②：未开盘快照过滤——price==昨收且无有效今开 → 剔除该票。
    """
    out = {}
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}),
                timeout=8).read().decode("gbk", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for line in raw.strip().splitlines():
            m = re.match(r"v_(\w+)=\"(.*)\"", line.strip())
            if not m:
                continue
            full, parts = m.group(1), m.group(2).split("~")
            if len(parts) < 6:
                continue
            try:
                price, prev, opx = float(parts[3]), float(parts[4]), float(parts[5])
            except ValueError:
                continue
            # 未开盘过滤：今开无效（0 或等于昨收）且现价==昨收 → 非今日状态
            open_pct = round((opx / prev - 1) * 100, 2) if prev > 0 and opx > 0 else None
            if open_pct is None and prev > 0 and abs(price - prev) < 0.001:
                continue
            out[full] = {"price": price, "prev": prev, "open": opx,
                         "open_pct": open_pct}
    return out


# ---------------------------------------------------------------------------
# 数据质量防线（规格 2.3）
# ---------------------------------------------------------------------------

TOTAL_AMT_ABS = (0.8e12, 8e12)  # 日总额绝对区间（元）


def check_total_amount(amount_yuan):
    lo, hi = TOTAL_AMT_ABS
    if not (lo <= amount_yuan <= hi):
        raise ValueError(f"日总额 {amount_yuan:.2e} 出界 [{lo:.1e},{hi:.1e}]，数据异常拒写")


def corp_action_scan(rows):
    """除权五重判据：跳变≥15% + 相邻交易日 + |pct|≤12% + ratio/pct 背离≥10pp + vol>0。"""
    flags = []
    for i in range(1, len(rows)):
        d0, o0, c0, h0, l0, v0 = rows[i - 1]
        d1, o1, c1, h1, l1, v1 = rows[i]
        gap_days = (datetime.strptime(d1, "%Y-%m-%d")
                    - datetime.strptime(d0, "%Y-%m-%d")).days
        jump = abs(o1 / c0 - 1) if c0 > 0 else 0
        pct = abs(c1 / c0 - 1) if c0 > 0 else 0
        diverge = abs(jump - pct)
        if jump >= 0.15 and gap_days <= 4 and pct <= 0.12 \
                and diverge >= 0.10 and v1 > 0:
            flags.append((d1, jump))
    return flags


def upsert_klines(con, code, rows):
    """写日K（调用方须先过交易日守门）；返回写入行数。"""
    n = 0
    prev_c = None
    for d, o, c, h, l, v in rows:
        pct = round((c / prev_c - 1) * 100, 2) if prev_c else None
        prev_c = c
        con.execute(
            "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, d, o, h, l, c, v, None, pct, None))
        n += 1
    con.commit()
    return n


def repair_pair_units(con, float_shares, today):
    """量价锚修复：q = vol/流通股本 > 0.01 为锚，检测全市场量纲失灵并幂等修复。"""
    rows = con.execute(
        "SELECT code, v FROM klines WHERE date=? AND v>0", (today,)).fetchall()
    bad = [(code, v) for code, v in rows
           if code in float_shares and v / float_shares[code] > 0.01]
    if len(bad) > len(rows) * 0.5:  # 全市场失灵
        for code, v in bad:
            con.execute("UPDATE klines SET v=v/100 WHERE code=? AND date=?",
                        (code, today))
        con.commit()
        return len(bad)
    return 0
