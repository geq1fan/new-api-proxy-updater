# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

这是一个 New-API 渠道代理更新器，用于定时更新指定渠道的代理IP。项目从 GitHub 获取代理列表，测试延迟，选择最佳代理，并更新到 New-API 实例中。

## 核心功能

### 代理测试系统
- **传统模式**: 使用 `find_best_proxy_by_latency()` 函数进行基础延迟测试
- **增强模式**: 使用 `find_best_proxy_by_latency_enhanced()` 函数提供多维度统计分析

### 核心类架构
1. **TestConfig**: 测试配置管理，处理环境变量配置
2. **ErrorHandler**: 错误处理和降级策略管理
3. **LatencyStatistics**: 延迟统计计算器，提供全面的分析指标
4. **ProxyEvaluator**: 代理评估器，提供综合评分和排序功能
5. **EnhancedLatencyTester**: 增强的延迟测试器，集成所有功能

### 关键函数
- `extract_proxies_by_region()`: 从Markdown文本中提取指定地区代理
- `test_proxy_connectivity()`: 测试单个代理连通性
- `measure_proxy_latency()`: 精确测量代理延迟
- `update_channel_proxy()`: 更新New-API渠道代理设置
- `main()`: 主执行函数

### 缓存管理函数
- `get_cached_proxy_list()`: 从缓存文件读取代理列表内容
- `save_proxy_list_cache()`: 保存代理列表内容到缓存文件
- `load_proxy_list_from_cache()`: 从缓存加载代理列表作为备用方案

## 常用命令

### 运行脚本
```bash
# 安装依赖
pip install -r script/requirements.txt

# 运行代理更新脚本
python script/proxy_updater.py
```

### 单元测试
```bash
# 运行所有测试
python -m unittest script.test_enhanced_proxy -v

# 运行特定测试类
python -m unittest script.test_enhanced_proxy.TestLatencyStatistics -v

# 运行特定测试方法
python -m unittest script.test_enhanced_proxy.TestLatencyStatistics.test_basic_stats_calculation -v
```

## 环境变量配置

### 基础配置
- `BASE_URL`: New-API实例URL
- `ADMIN_ID`: 管理员用户ID
- `ADMIN_TOKEN`: 管理员访问令牌
- `CHANNEL_IDS`: 需要更新的渠道ID列表（JSON数组格式）
- `PROXY_REGION`: 代理地区（默认：香港）

### 测试配置
- `MAX_PROXY_TEST_COUNT`: 最大代理测试数量（默认：5）
- `MIN_SUCCESS_RATE`: 最低成功率要求（默认：0.8）
- `MAX_LATENCY_MS`: 最大可接受延迟（默认：5000ms）
- `TEST_COUNT`: 每个URL的采样次数（默认：3）
- `TEST_TIMEOUT`: 请求超时秒数（默认：10）
- `TEST_CONCURRENCY`: 并发工作线程数（默认：5）
- `TEST_URLS`: 自定义测试URL（逗号分隔）

### 增强评估配置
- `LATENCY_TEST_SAMPLES`: 延迟测试样本数（默认：5）
- `LATENCY_TEST_URLS_COUNT`: 测试URL数量（默认：3）
- `WEIGHT_PERFORMANCE`: 性能权重（默认：0.4）
- `WEIGHT_STABILITY`: 稳定性权重（默认：0.35）
- `WEIGHT_AVAILABILITY`: 可用性权重（默认：0.25）

## 代码架构说明

### 延迟测试流程
1. 从GitHub获取代理列表Markdown文件
2. 保存代理列表内容到本地缓存文件
3. 解析指定地区的代理
4. 并发测试代理延迟（支持传统和增强模式）
5. 根据评分选择最佳代理
6. 更新New-API渠道设置
7. 记录任务完成状态

### 缓存机制
- **代理列表缓存**: 每次成功获取代理列表后都会保存到 `proxy_list_cache.json`
- **失败回退**: 当GitHub请求失败时，自动使用上次的缓存文件继续执行
- **移除MD5检查**: 不再根据内容变化跳过执行，每次都运行完整流程

### 增强统计指标
- **基础统计**: 平均值、中位数、百分位数等
- **变异性指标**: 标准差、变异系数、四分位距等
- **稳健性指标**: 一致性评分、异常值比例等
- **API性能指标**: 突发延迟率、超时风险评分等

### 评分算法
综合评分 = 性能评分×40% + 稳定性评分×35% + 可用性评分×25%

## 注意事项

1. **缓存机制**: 使用代理列表缓存文件确保任务连续性，GitHub请求失败时自动回退到缓存
2. **降级策略**: 当测试样本不足时自动切换到更可靠的测试URL
3. **并发优化**: 使用ThreadPoolExecutor实现高效的并发测试
4. **错误处理**: 完善的异常处理和日志记录
5. **向后兼容**: 保持与传统 `find_best_proxy_by_latency()` 函数的完全兼容
6. **执行保证**: 每次都执行完整流程，不再因内容未变化而跳过

## 开发指南

- 修改测试配置时，确保同时更新TestConfig类和相关文档
- 添加新的统计指标时，需要在LatencyStatistics类中添加对应方法
- 修改评分算法时，需要更新ProxyEvaluator类的权重配置
- 所有环境变量都应有合理的默认值