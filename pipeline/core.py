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


def is_trading_day(con, date):
    return date in trade_calendar(con)


def is_trading_day_cross(con, date):
    """M04：交易日历交叉确认——单一指数日K不是唯一权威。
    指数日K命中 且 当日全市场快照 pct 非全零 → 交易日。
    返回 (certain: bool, reason)。"""
    if date not in trade_calendar(con):
        return False, "指数日K无此日期"
    row = con.execute(
        "SELECT COUNT(*), SUM(CASE WHEN ABS(COALESCE(pct,0))>0.0001 THEN 1 "
        "ELSE 0 END) FROM snapshot WHERE date=?", (date,)).fetchone()
    total, nonzero = row[0], row[1] or 0
    if total and nonzero == 0:
        return False, "快照 pct 全零：疑似休市日"
    return True, "指数日K与快照交叉确认"


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
# 裸域名 ifzq 与 web.ifzq 是独立 WAF 策略：2026-09-13 实测 web 被封时裸域名可用
TX_HOST = "ifzq.gtimg.cn"


def limiter(host):
    with _M:
        return _LIMITERS.setdefault(host, RateLimiter())


def guard(host):
    with _M:
        return _GUARDS.setdefault(host, SourceGuard())


def guards_health():
    return {h: g.health() for h, g in _GUARDS.items()}


def fetch_text(url, timeout=10, retries=2, referer=None):
    """带浏览器 UA 的 GET；限流/失败计入 guard；BanBlocked 短路。
    referer：部分源（EM 数据中心）强制校验 Referer，缺失返回 403。"""
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
    url = (f"https://{TX_HOST}/appstock/app/fqkline/get?"
           f"param={pfx}{num},day,,,{days},qfq")
    js = json.loads(fetch_text(url))
    node = js.get("data", {}).get(f"{pfx}{num}", {})
    rows = node.get("qfqday") or node.get("day") or []
    return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]),
             float(r[5])] for r in rows if len(r) >= 6]


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


def kline_batch(codes, days=260, con=None, workers=6):
    """③ 双源轮转 + 双通道并发（吸收原项目 backfill.py）。

    东财/腾讯交错各领一半，互不抢同一 host 令牌桶；任一通道主源
    熔断（BanBlocked）→ 本票直接改走对侧；对侧也熔断 → 放弃本票
    留待下轮（宁可缺数据也不打封禁源）。RateLimiter/SourceGuard
    线程安全，聚合速率仍受每 host 10 req/s 约束。"""
    em_g, tx_g = guard(EM_HOST), guard(TX_HOST)
    if em_g.blocked() and tx_g.blocked():
        return {}
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    out_lock = threading.Lock()

    def pull(kind, num, pfx):
        primary = _kline_one_em if kind == "em" else _kline_one_tx
        secondary = _kline_one_tx if kind == "em" else _kline_one_em
        rows = None
        try:
            rows = primary(num, pfx, days)
        except BanBlocked:
            rows = None
        except Exception:  # noqa: BLE001 — 普通失败也切对侧
            rows = None
        if not rows:
            try:
                rows = secondary(num, pfx, days)
            except Exception:  # noqa: BLE001
                rows = None
        if not rows:
            try:
                rows = _kline_one_sina(num, pfx, days)   # 第三独立源兜底
            except Exception:  # noqa: BLE001
                rows = None
        if rows:
            with out_lock:
                out[num] = rows

    def run_channel(kind, items):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda it: pull(kind, it[0], it[1]), items))

    jobs = list(codes)          # 每项 = (num, pfx)
    th1 = threading.Thread(target=run_channel, args=("em", jobs[0::2]))
    th2 = threading.Thread(target=run_channel, args=("tx", jobs[1::2]))
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
