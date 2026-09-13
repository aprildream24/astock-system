import json
import sys
import urllib.request

TOKEN = sys.argv[1]
HDR = {"Authorization": f"token {TOKEN}", "User-Agent": "astra",
       "Accept": "application/vnd.github+json"}
req = urllib.request.Request(
    "https://api.github.com/repos/aprildream24/astock-system/commits?per_page=3",
    headers=HDR)
d = json.loads(urllib.request.urlopen(req, timeout=30).read())
for c in d:
    print(c["sha"][:10], c["commit"]["message"][:50])
req = urllib.request.Request(
    "https://api.github.com/repos/aprildream24/astock-system/contents/",
    headers=HDR)
d = json.loads(urllib.request.urlopen(req, timeout=30).read())
print("根目录:", sorted(x["name"] for x in d))
