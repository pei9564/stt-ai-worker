"""透過 nvidia-smi 讀取 GPU 即時狀態，供 UI 輪詢。"""

from __future__ import annotations

import subprocess

_QUERY = "utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu"


def read_gpu_stats() -> dict:
    """回傳 GPU 使用率 / VRAM / 功耗 / 溫度。

    讀不到 GPU (容器沒掛 GPU、沒有 nvidia-smi 等) 時回 `{"available": False, ...}`，
    呼叫端不需處理例外。
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return {"available": False, "error": out.stderr.strip()[-200:]}
        util, mem_used, mem_total, power, temp = (
            x.strip() for x in out.stdout.strip().split(",")
        )
        return {
            "available": True,
            "util": float(util),
            "mem_used": float(mem_used),
            "mem_total": float(mem_total),
            "power": None if power.upper().startswith("N/A") else float(power),
            "temp": float(temp),
        }
    except Exception as e:  # nvidia-smi 不存在 / timeout / 解析失敗
        return {"available": False, "error": str(e)}
