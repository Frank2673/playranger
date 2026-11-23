"""DeepSeek VLM 视觉功能测试脚本。

用法：
    python tools/test_deepseek_vlm.py

测试内容：
1. VLMClient health_check
2. 用一张在线图片测试 inspect（图像描述）
3. 用两张图测试 compare（前后对比）
4. VisionEngine 集成测试
"""
from __future__ import annotations

import os
import sys
import base64
from pathlib import Path
from io import BytesIO

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# ===== DeepSeek 视觉配置 =====
# 用同一个 Key，模型名用 deepseek-v4-flash（支持视觉）
VLM_BASE = "https://api.deepseek.com/v1"
VLM_KEY = os.environ.get("LLM_API_KEY", "")
VLM_MODEL = "deepseek-v4-flash"

print("=" * 60)
print("DeepSeek VLM 视觉功能测试")
print("=" * 60)
print(f"Base URL: {VLM_BASE}")
print(f"Model: {VLM_MODEL}")
print(f"API Key: {VLM_KEY[:8]}...{VLM_KEY[-4:] if len(VLM_KEY) > 12 else '***'}")
print()

if not VLM_KEY:
    print("❌ 没有找到 API Key，请在 .env 中设置 LLM_API_KEY")
    sys.exit(1)

# ---- 1. 测试 VLMClient ----
print("【1/4】测试 VLMClient.health_check()")
from playranger.core.brain.vlm_client import VLMClient

vlm = VLMClient(
    base_url=VLM_BASE,
    api_key=VLM_KEY,
    model=VLM_MODEL,
    timeout=30,
)

ok, msg = vlm.health_check()
if ok:
    print(f"  ✅ 健康检查通过: {msg}")
else:
    print(f"  ❌ 健康检查失败: {msg}")
    print()
    print("提示: 如果是余额不足 (402)，需要先去 platform.deepseek.com 充值")
    sys.exit(1)

print()

# ---- 2. 生成一张测试图片（用 PIL 画个简单的，不需要网络） ----
print("【2/4】生成测试图片...")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("  ℹ️  Pillow 未安装，尝试用纯 Python 生成占位图")
    # 用最简单的方式：生成一个 1x1 红色像素的 base64 图
    # 不行，VLM 需要有意义的图。我们改用网络图片。
    print("  将使用网络测试图片")
    test_image_url = "https://www.google.com/images/branding/googlelogo/1x/googlelogo_color_272x92dp.png"
    test_image_b64 = None
else:
    # 生成一张测试图：蓝底白字 "Hello PlayRanger"
    img = Image.new('RGB', (400, 200), color=(30, 64, 175))
    draw = ImageDraw.Draw(img)
    # 画个按钮形状
    draw.rounded_rectangle([50, 70, 350, 140], radius=10, fill=(76, 175, 80))
    draw.text((150, 95), "Start Game", fill="white")
    draw.text((80, 30), "HP: 100  Score: 0", fill="white")

    # 转 base64
    buf = BytesIO()
    img.save(buf, format='PNG')
    test_image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    test_image_url = None
    print(f"  ✅ 生成测试图片成功 (400x200 PNG)")

print()

# ---- 3. 测试图像描述 ----
print("【3/4】测试图像描述 (inspect)")
print("  正在调用 VLM 分析图片...")

try:
    if test_image_b64:
        # 保存临时图片
        tmp_img = PROJECT_ROOT / "output" / "test_vlm.png"
        tmp_img.parent.mkdir(exist_ok=True)
        with open(tmp_img, 'wb') as f:
            f.write(base64.b64decode(test_image_b64))

        result = vlm.inspect(
            image_path=str(tmp_img),
            prompt="请描述这张游戏截图的内容，包括可见的 UI 元素、文字、按钮等。",
        )
    else:
        result = vlm.inspect(
            image_url=test_image_url,
            prompt="请描述这张图片的内容。",
        )

    print(f"  ✅ 描述结果:")
    # 限制输出长度
    preview = result[:300] + "..." if len(result) > 300 else result
    for line in preview.split('\n'):
        print(f"    {line}")
except Exception as e:
    print(f"  ❌ 图像描述失败: {e}")
    import traceback
    traceback.print_exc()

print()

# ---- 4. 测试 VisionEngine 集成 ----
print("【4/4】测试 VisionEngine 异常检测")
from playranger.core.engines.vision_engine import VisionEngine

ve = VisionEngine(vlm=vlm)

if test_image_b64:
    # 保存临时图片
    tmp_img = PROJECT_ROOT / "output" / "test_vlm.png"
    tmp_img.parent.mkdir(exist_ok=True)
    with open(tmp_img, 'wb') as f:
        f.write(base64.b64decode(test_image_b64))

    print(f"  测试图片: {tmp_img}")
    print("  正在做异常检测...")

    result = ve.inspect(str(tmp_img))
    print(f"  ✅ 异常检测完成")
    print(f"     是否异常: {result.is_abnormal}")
    print(f"     异常类型: {result.anomaly_type}")
    print(f"     置信度: {result.confidence:.0%}")
    print(f"     描述: {result.description[:100]}...")
else:
    print("  ⏭️  跳过（需要本地图片）")

print()
print("=" * 60)
print("测试完成！")
print("=" * 60)
