# -*- coding: utf-8 -*-
"""模拟盘版式样例推送（CI rehearsal 用，2026-09-22 用户要求看到模板）。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import executor, notifier  # noqa: E402
from pipeline.core import get_conn, today_str  # noqa: E402

con = get_conn()
today = today_str()
executor.ensure_account(con, today)
acct = executor.account_snapshot(con, today)
hrows = executor.holdings_rows(con, today)
html = notifier.render_exec_report(
    today, acct, opened=[], blocked=[], sold=[], holdings=hrows,
    skipped=[], note="模拟盘版式样例（演练；节假日无建仓属正常）",
    slot_label=" · 演练")
html += ('<div style="margin-top:14px;padding-top:8px;border-top:1px solid '
         '#e5e5e5;color:#576b95;font-size:13px">🌐 网页端：'
         '<a href="https://aprildream24.github.io/astock-system/">'
         'aprildream24.github.io/astock-system</a></div>')
r = notifier.push("rehearsal_exec", "模拟盘版式样例", html,
                  date=today, force=True)
print(f"[rehearsal] exec sample push: {r.get('status')}")
