"""本机资源探测与并发建议。

公式（可在控制台复算）：
    recommended = floor((available_gb - reserve_gb) * 1024 / per_worker_mb)
    recommended = clamp(recommended, 1, min(8, cpu_count))
"""

from __future__ import annotations

import os

HARD_MAX_WORKERS = 8
DEFAULT_RESERVE_GB = 2.0
DEFAULT_PER_WORKER_MB = 1536


def per_worker_mb() -> int:
    try:
        return max(512, int(os.environ.get("PLAYRANGER_PER_WORKER_MB", DEFAULT_PER_WORKER_MB)))
    except ValueError:
        return DEFAULT_PER_WORKER_MB


def reserve_gb() -> float:
    try:
        return max(0.5, float(os.environ.get("PLAYRANGER_RESERVE_GB", DEFAULT_RESERVE_GB)))
    except ValueError:
        return DEFAULT_RESERVE_GB


def hard_max_workers() -> int:
    """并发硬上限；可用 PLAYRANGER_MAX_WORKERS 放宽（1-32）。"""
    try:
        return max(1, min(32, int(os.environ.get("PLAYRANGER_MAX_WORKERS", HARD_MAX_WORKERS))))
    except ValueError:
        return HARD_MAX_WORKERS


def compute_recommendation(
    available_gb: float,
    cpu_count: int,
    *,
    per_worker_mb_value: int | None = None,
    reserve_gb_value: float | None = None,
    hard_max: int | None = None,
) -> int:
    """纯函数，便于单测与前端复算。"""
    budget_mb = (max(0.0, float(available_gb)) - max(0.0, float(reserve_gb_value if reserve_gb_value is not None else reserve_gb()))) * 1024
    worker_mb = max(256, int(per_worker_mb_value or per_worker_mb()))
    by_mem = int(budget_mb // worker_mb)
    by_cpu = max(1, int(cpu_count))
    cap = int(hard_max if hard_max is not None else hard_max_workers())
    return max(1, min(cap, by_cpu, by_mem if by_mem > 0 else 1))


def snapshot() -> dict:
    """返回控制台系统卡所需数据；psutil 缺失时给出明确降级。"""
    data: dict = {
        "total_gb": 0.0,
        "available_gb": 0.0,
        "used_percent": 0.0,
        "cpu_count": os.cpu_count() or 1,
        "per_worker_mb": per_worker_mb(),
        "reserve_gb": reserve_gb(),
        "hard_max": hard_max_workers(),
        "recommended": 1,
        "rss_mb": 0.0,
        "memory_source": "unavailable",
    }
    try:
        import psutil

        mem = psutil.virtual_memory()
        data.update({
            "total_gb": round(mem.total / 1024**3, 2),
            "available_gb": round(mem.available / 1024**3, 2),
            "used_percent": round(float(mem.percent), 1),
            "memory_source": "psutil",
        })
        try:
            data["rss_mb"] = round(psutil.Process().memory_info().rss / 1024**2, 1)
        except Exception:
            pass
    except Exception:
        # 兜底：Windows 下用 GlobalMemoryStatusEx，避免系统卡完全空白
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                data.update({
                    "total_gb": round(stat.ullTotalPhys / 1024**3, 2),
                    "available_gb": round(stat.ullAvailPhys / 1024**3, 2),
                    "used_percent": round(float(stat.dwMemoryLoad), 1),
                    "memory_source": "win32",
                })
        except Exception:
            pass

    data["recommended"] = compute_recommendation(
        data["available_gb"],
        data["cpu_count"],
        per_worker_mb_value=data["per_worker_mb"],
        reserve_gb_value=data["reserve_gb"],
    )
    return data
