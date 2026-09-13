import json
import os
import sys

sys.path.insert(0, ".")
from pipeline import notifier
from pipeline.core import get_conn

con = get_conn()
con.execute("DELETE FROM push_ledger WHERE mode IN ('build_close','watch_advice')")
con.commit()
con.close()
p = notifier.DIST_LEDGER
if os.path.exists(p):
    with open(p, encoding="utf-8") as f:
        dist = json.load(f)
    for k in [k for k, v in dist.items()
              if v.get("mode") in ("build_close", "watch_advice")]:
        del dist[k]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(dist, f, ensure_ascii=False, indent=1)
print("cleared")
