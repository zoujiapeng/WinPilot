import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from winpilot.perception import search

t = time.time()
r = search.search("Vintersaga", max_results=10)
dt = (time.time() - t) * 1000
print(f"引擎={r['engine']} 命中={r['count']} 耗时={dt:.0f}ms")
for p in r["results"][:5]:
    print("  ", p)

# 全盘搜一个常见扩展名，验证不再超时
t = time.time()
r2 = search.search("*.exe", max_results=20)
dt = (time.time() - t) * 1000
print(f"全盘*.exe 引擎={r2['engine']} 命中={r2['count']} 耗时={dt:.0f}ms")
assert r["engine"] == "everything", f"应走 everything，实际 {r['engine']}"
print("SEARCH EVERYTHING OK")
