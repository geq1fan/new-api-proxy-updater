#!/bin/bash

# 设置环境变量示例
export BASE_URL=""
export ADMIN_ID="1"
export ADMIN_TOKEN=""
export CHANNEL_IDS="[1]"
export PROXY_REGION="美国"

# 安装依赖
pip install -r requirements.txt

# 运行脚本
cd "$(dirname "$0")"
python proxy_updater.py