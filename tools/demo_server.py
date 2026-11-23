"""一键启动「演示控制台」—— 录屏 / Demo 专用。

为什么需要这个启动器：
    直接在 PowerShell 里用 $env:PLAYRANGER_DB_PATH 传含中文的绝对路径，
    在部分代码页下会被损坏，导致 SQLite "unable to open database file"。
    本脚本在 Python 进程内用 Unicode 字符串设置环境变量，彻底绕开该问题。

做的事：
    1. chdir 到项目根；
    2. 指向独立演示库 output/demo/（与真实 output/platform.db 隔离）；
    3. 默认 Edge 作为 Playwright 浏览器；
    4. 演示库不存在或带 --seed 时，自动跑 tools/seed_demo.py 生成数据；
    5. 启动 uvicorn，控制台地址 http://127.0.0.1:8000/app/

用法：
    python tools/demo_server.py            # 首次自动播种
    python tools/demo_server.py --seed     # 强制重置演示数据
    python tools/demo_server.py --port 8020
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_DB = ROOT / "output" / "demo" / "platform.db"
DEMO_TASKS = ROOT / "output" / "demo" / "tasks"

# 进程内设置（Unicode 安全，不经过 shell 代码页）
os.environ["PLAYRANGER_DB_PATH"] = str(DEMO_DB)
os.environ["PLAYRANGER_TASKS_DIR"] = str(DEMO_TASKS)
os.environ.setdefault(
    "PLAYWRIGHT_CHROMIUM_PATH",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
os.environ.setdefault("PLAYRANGER_HEADLESS", "1")

port = 8000
if "--port" in sys.argv:
    port = int(sys.argv[sys.argv.index("--port") + 1])

force_seed = "--seed" in sys.argv
if force_seed or not DEMO_DB.exists():
    print("▶ 生成演示数据 ...")
    subprocess.run([sys.executable, str(ROOT / "tools" / "seed_demo.py")], check=True)
    print()

print("=" * 60)
print("  PlayRanger 演示控制台启动中")
print("=" * 60)
print(f"  控制台 : http://127.0.0.1:{port}/app/")
print(f"  API 文档: http://127.0.0.1:{port}/docs")
print(f"  账号   : demo / demo1234")
print(f"  演示库 : {DEMO_DB}")
print("  停止   : Ctrl+C")
print("=" * 60)

import uvicorn  # noqa: E402
from playranger.api.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
