"""Write a synthetic 1-minute logger CSV: make_csv.py <out> <days> [end_epoch]. Realistic column set (no money)."""
import math
import random
import sys
import time
from datetime import datetime, timezone

out, days = sys.argv[1], int(sys.argv[2])
end = int(sys.argv[3]) if len(sys.argv) > 3 else (int(time.time()) // 86400) * 86400 - 86400 * 2
start = end - days * 86400
random.seed(1)
cols = ["ts", "grid_present", "grid_power_w", "grid_import_wh", "grid_export_wh", "grid_voltage_v", "pv_power_w", "load_power_w",
        "battery_power_w", "battery_voltage_v", "battery_current_a", "battery_temp_c", "battery_soc_bms_pct", "genset_running", "genset_power_w",
        "genset_fuel_l", "inverter_mode_active"]
soc = 60.0
with open(out, "w", newline="") as f:
    f.write(",".join(cols) + "\n")
    for t in range(start, end, 60):
        h = ((t + 5 * 3600) % 86400) / 3600
        pv = max(0.0, math.sin(math.pi * (h - 6) / 12)) * 80000 * (0.85 + 0.15 * random.random()) if 6 < h < 18 else 0.0
        load = 60000 + 20000 * math.sin(math.pi * h / 24) + 5000 * random.random()
        bat = max(-30000, min(30000, (pv - load) * 0.5))
        soc = max(10, min(100, soc + bat / 60 / 200000 * 100))
        grid = load - pv - (-bat if bat < 0 else 0)
        present = 0 if (17 <= h < 18 and (t // 86400) % 3 == 0) else 1
        row = [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), present, round(grid if present else 0, 1),
               round(max(grid, 0) / 60, 3) if present else 0, round(max(-grid, 0) / 60, 3) if present else 0, 231.4 if present else 0,
               round(pv, 1), round(load, 1), round(bat, 1), 51.2, round(bat / 51.2, 2), 31.5, round(soc, 2), 0, 0, 0, "hold"]
        f.write(",".join(str(x) for x in row) + "\n")
print(out, days, "days")
