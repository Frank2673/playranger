"""PlayRanger 试玩测试平台 —— 账号、调度、执行、修复沙箱。

与既有 CLI/SDK 能力的关系：
- 复用 core.engines / core.agent 的真实浏览器执行链路；
- 本包只负责「多用户 + 任务队列 + 持久化 + 控制台 API」这一层。
"""
