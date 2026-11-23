"""页面稳定等待策略与随机种子注入。

把「固定 sleep」替换成可观察的稳定条件：

1. 网络空闲（尽力而为，最多 300ms，失败忽略）
2. 连续 N 帧 DOM 无变更（MutationObserver + requestAnimationFrame）
3. 没有仍在运行的动画（document.getAnimations）

任何异常都降级为一次短暂等待，绝不阻断主流程。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# ── 常量 ────────────────────────────────────────────────────

DEFAULT_FRAMES = 2
DEFAULT_TIMEOUT_MS = 1500
DEFAULT_NETWORK_IDLE_MS = 300
FALLBACK_WAIT_MS = 80

SEED_GLOBAL_NAME = "__PLAYRANGER_SEED__"


# ── 数据结构 ────────────────────────────────────────────────


@dataclass
class SettleResult:
    """一次页面稳定等待的结果。"""

    status: str          # stable / timeout / unsupported / error
    frames: int = 0      # 连续稳定帧数
    animations: int = 0  # 结束时仍在运行的动画数
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── 注入脚本 ────────────────────────────────────────────────

SETTLE_SCRIPT = """
([timeoutMs, requiredFrames]) => new Promise((resolve) => {
  const started = performance.now();
  let frames = 0;
  let mutations = 0;
  let finished = false;
  let observer = null;

  const runningAnimations = () => {
    try {
      return (document.getAnimations ? document.getAnimations() : [])
        .filter((a) => a.playState === 'running').length;
    } catch (e) {
      return 0;
    }
  };

  const finish = (status) => {
    if (finished) return;
    finished = true;
    if (observer) {
      try { observer.disconnect(); } catch (e) {}
    }
    resolve({
      status: status,
      frames: frames,
      animations: runningAnimations(),
      elapsed_ms: Math.round(performance.now() - started),
    });
  };

  try {
    observer = new MutationObserver(() => { mutations += 1; });
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  } catch (e) {
    observer = null;
  }

  const tick = () => {
    if (finished) return;
    const running = runningAnimations();
    if (mutations === 0 && running === 0) {
      frames += 1;
    } else {
      frames = 0;
      mutations = 0;
    }
    if (frames >= requiredFrames) { finish('stable'); return; }
    if (performance.now() - started >= timeoutMs) { finish('timeout'); return; }
    requestAnimationFrame(tick);
  };

  requestAnimationFrame(tick);
  setTimeout(() => finish('timeout'), timeoutMs + 50);
})
"""


def build_seed_script(seed: int) -> str:
    """生成注入随机种子的初始化脚本。

    在页面加载前注入 ``window.__PLAYRANGER_SEED__``，供游戏自行读取；
    不支持 SDK setSeed 的游戏也能获得一个稳定的种子来源。
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed 必须是整数，收到 {type(seed).__name__}: {seed!r}")
    return f"window.{SEED_GLOBAL_NAME} = {seed};"


# ── 等待实现 ────────────────────────────────────────────────


def settle(
    page: Any,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    *,
    required_frames: int = DEFAULT_FRAMES,
    network_idle_ms: int = DEFAULT_NETWORK_IDLE_MS,
) -> SettleResult:
    """等待页面进入稳定状态。

    Args:
        page: Playwright Page（或测试替身）
        timeout_ms: 稳定等待硬超时
        required_frames: 连续稳定帧数
        network_idle_ms: 网络空闲等待上限；0 表示跳过

    Returns:
        SettleResult；任何异常都返回 ``status="error"``，不抛出。
    """
    timeout_ms = max(1, int(timeout_ms))
    required_frames = max(1, int(required_frames))

    try:
        if network_idle_ms > 0:
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=min(int(network_idle_ms), timeout_ms)
                )
            except Exception:
                pass

        data = page.evaluate(SETTLE_SCRIPT, [timeout_ms, required_frames])
        if not isinstance(data, dict):
            return SettleResult("unsupported", 0, 0, 0)
        return SettleResult(
            status=str(data.get("status") or "unsupported"),
            frames=int(data.get("frames") or 0),
            animations=int(data.get("animations") or 0),
            elapsed_ms=int(data.get("elapsed_ms") or 0),
        )
    except Exception:
        try:
            page.wait_for_timeout(FALLBACK_WAIT_MS)
        except Exception:
            pass
        return SettleResult("error", 0, 0, 0)


__all__ = [
    "DEFAULT_FRAMES",
    "DEFAULT_NETWORK_IDLE_MS",
    "DEFAULT_TIMEOUT_MS",
    "FALLBACK_WAIT_MS",
    "SEED_GLOBAL_NAME",
    "SETTLE_SCRIPT",
    "SettleResult",
    "build_seed_script",
    "settle",
]
