#!/bin/bash
# PlayRanger — 单台 DGX Spark 一键部署脚本
# 用法：bash scripts/deploy_spark.sh
#
# 注意：Day1 只是占位，Day 10 左右补全真实部署逻辑。

set -e

echo "=== PlayRanger DGX Spark 部署脚本 ==="
echo "注意：此脚本需在 DGX Spark 机器上执行"
echo ""

# TODO: Day 10 补全
# 1. 安装 SGLang
# 2. 启动 LLM (Qwen3-8B) 端口 30000
# 3. 启动 VLM (Qwen2.5-VL-7B) 端口 30001
# 4. 启动健康检查代理 端口 8080

echo "[占位] DGX Spark 部署脚本，Day 10 实现完整版本"
echo "部署完成后验证："
echo "  curl http://localhost:8080/health"
