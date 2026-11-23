"""PlayRanger SDK — Python 封装。

提供两种使用方式：

1. **远程客户端** (`PlayRangerClient`) — 通过 HTTP API 调用服务端
   适合多语言、分布式、服务端部署

   ```python
   from playranger.sdk import PlayRangerClient

   client = PlayRangerClient("http://localhost:8000")
   test = client.start_test(game_url="http://...")
   ```

2. **嵌入式** (`PlayRanger`) — 直接使用本地核心模块
   适合脚本化、快速原型、CI/CD 集成

   ```python
   from playranger.sdk import PlayRanger

   pr = PlayRanger()
   result = pr.run_test(game_url="http://...")
   ```
"""

from playranger.sdk.client import PlayRangerClient
from playranger.sdk.embedded import PlayRanger, EmbeddedTestResult
from playranger.sdk.models import (
    BugDetail,
    BugList,
    BugSummary,
    Capabilities,
    Health,
    ReplayResult,
    StepList,
    StepRecord,
    TestInfo,
)

__all__ = [
    # 客户端
    "PlayRangerClient",
    # 嵌入式
    "PlayRanger",
    "EmbeddedTestResult",
    # 数据模型
    "Health",
    "Capabilities",
    "TestInfo",
    "StepList",
    "StepRecord",
    "BugList",
    "BugSummary",
    "BugDetail",
    "ReplayResult",
]
