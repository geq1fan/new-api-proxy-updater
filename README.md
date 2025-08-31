# New-API Channel Proxy Updater

A worker that can setup a cron job to periodically update the proxy IP for specific channels.

The proxy source: https://github.com/TopChina/proxy-list/blob/main/README.md

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/BHznJNs/new-api-proxy-updater)

## 新增功能：增强的代理延迟评估

本项目已增强了代理延迟评估功能，包括：

### 📊 多维度统计指标
- **基础统计**: 平均值、中位数、最值、百分位数（P25, P75, P95, P99）
- **变异性指标**: 标准差、变异系数、四分位距、中位数绝对偏差
- **稳健性指标**: 一致性评分、稳定性指数、异常值比例、截尾平均数
- **API性能指标**: 突发延迟率、超时风险评分、QoS综合评分、持续性能评分

### 🏆 智能评分算法
- **综合评分算法**: 性能（40%）+ 稳定性（35%）+ 可用性（25%）
- **多维度评估**: 结合延迟、稳定性和可用性的综合评估
- **自适应排序**: 根据多个指标智能排序选择最佳代理

### 🔄 增强测试策略
- **URL分类测试**: 支持快速、标准、重负载和混合模式
- **降级策略**: 自动识别失败场景并使用更可靠的测试方法
- **错误处理**: 智能判断可恢复错误并采取适当对策
- **并发优化**: 使用 ThreadPoolExecutor 实现高效并发测试

### 🔧 向后兼容性
- **API兼容**: 保持与现有 `find_best_proxy_by_latency()` 函数的完全兼容
- **配置兼容**: 所有现有环境变量继续有效
- **渐进升级**: 可选择使用增强功能，不影响现有部署

## 环境变量

### 基础配置

| Variable Name | Description | Default | Example |
|    ---        | ---         | ---     | ---     |
| `BASE_URL`    | The URL of your New-API instance | - | https://new-api.com |
| `ADMIN_ID`    | The ID of your admin user account | - | `1` |
| `ADMIN_TOKEN` | The access token of your admin user account | - | `123456` |
| `CHANNEL_IDS` | The id of channels that is needed to update proxy IP | - | `[1, 2, 3, 4]` |
| `PROXY_REGION` | The region of the proxy to be used | `香港` | `香港` |
| `MAX_PROXY_TEST_COUNT` | Maximum number of proxies to test | `5` | `10` |
| `MIN_SUCCESS_RATE` | Minimum success rate requirement | `0.8` | `0.9` |
| `MAX_LATENCY_MS` | Maximum acceptable latency in milliseconds | `5000` | `3000` |
| `TEST_COUNT` | Number of test samples per URL | `3` | `5` |
| `TEST_TIMEOUT` | Request timeout in seconds | `10` | `15` |
| `TEST_CONCURRENCY` | Number of concurrent test threads | `5` | `8` |
| `TEST_URLS` | Custom test URLs (comma-separated) | 默认204端点 | `http://example.com,https://test.com` |

### 增强延迟评估配置

| Variable Name | Description | Default | Example |
|    ---        | ---         | ---     | ---     |
| `LATENCY_TEST_SAMPLES` | Number of latency test samples per URL | `5` | `10` |
| `LATENCY_TEST_URLS_COUNT` | Number of test URLs to use | `3` | `5` |
| `LATENCY_OUTLIER_THRESHOLD` | Outlier detection threshold (standard deviations) | `2.0` | `1.5` |
| `LATENCY_SPIKE_THRESHOLD_MS` | Spike latency threshold in milliseconds | `1000` | `800` |
| `LATENCY_CONSISTENCY_WINDOW` | Consistency measurement window | `0.3` | `0.2` |
| `WEIGHT_PERFORMANCE` | Weight for performance score (0-1) | `0.4` | `0.5` |
| `WEIGHT_STABILITY` | Weight for stability score (0-1) | `0.35` | `0.3` |
| `WEIGHT_AVAILABILITY` | Weight for availability score (0-1) | `0.25` | `0.2` |

## 使用说明

### 快速开始

1. **安装依赖**:
   ```bash
   pip install -r script/requirements.txt
   ```

2. **配置环境变量**:
   ```bash
   export BASE_URL="https://your-new-api.com"
   export ADMIN_ID="1"
   export ADMIN_TOKEN="your-admin-token"
   export CHANNEL_IDS="[1, 2, 3]"
   ```

3. **运行脚本**:
   ```bash
   python script/proxy_updater.py
   ```

### 运行单元测试

```bash
# 运行所有测试
python script/test_enhanced_proxy.py

# 运行特定测试类
python -m unittest script.test_enhanced_proxy.TestLatencyStatistics -v

# 运行特定测试方法
python -m unittest script.test_enhanced_proxy.TestLatencyStatistics.test_basic_stats_calculation -v
```

### 高级配置示例

```bash
# 增强测试配置
export LATENCY_TEST_SAMPLES=10
export LATENCY_TEST_URLS_COUNT=5
export WEIGHT_PERFORMANCE=0.5
export WEIGHT_STABILITY=0.3
export WEIGHT_AVAILABILITY=0.2

# 性能优化配置
export TEST_CONCURRENCY=10
export MAX_PROXY_TEST_COUNT=15
export TEST_TIMEOUT=15

# 自定义测试URL
export TEST_URLS="http://www.gstatic.com/generate_204,https://httpbin.org/status/200,https://api.github.com/zen"
```

## 功能特性

### 传统模式
- 使用 `find_best_proxy_by_latency()` 函数
- 基础的延迟测试和排序
- 兼容现有部署

### 增强模式
- 使用 `find_best_proxy_by_latency_enhanced()` 函数
- 多维度统计分析
- 智能评分算法
- 高级错误处理和降级策略

## 性能基准

基于单元测试的性能基准测试结果：

- **统计计算性能**: ~0.4秒 (100次迭代，1000个样本)
- **评估器性能**: ~0.002秒 (1000次迭代)
- **并发测试**: 支持高效的多线程并发测试

## 贡献指南

1. Fork 本仓库
2. 创建功能分支 (`git checkout -b feature/amazing-feature`)
3. 提交修改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 开启 Pull Request

## 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件获取详细信息。
