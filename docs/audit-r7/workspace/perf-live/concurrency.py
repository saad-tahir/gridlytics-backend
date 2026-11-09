"""Throughput of the single API worker under concurrent polling: concurrency.py <path> <threads> <seconds>"""
import sys
import threading
import time

sys.path.insert(0, "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit")
from api import client  # noqa: E402

path, n, secs = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
clients = [client("analyst") for _ in range(n)]
for cl in clients:
    cl.timeout = 120
lat = []
stop = time.time() + secs


def run(cl):
    while time.time() < stop:
        t = time.perf_counter()
        r = cl.get(path)
        lat.append((time.perf_counter() - t, r.status_code))


ths = [threading.Thread(target=run, args=(cl,)) for cl in clients]
t0 = time.time()
for th in ths:
    th.start()
for th in ths:
    th.join()
el = time.time() - t0
s = sorted(x[0] for x in lat)
print(f"{path} threads={n} n={len(s)} rps={len(s) / el:.1f} p50={s[len(s) // 2]:.3f} p95={s[int(len(s) * 0.95)]:.3f} max={s[-1]:.3f} codes={sorted(set(x[1] for x in lat))}")
