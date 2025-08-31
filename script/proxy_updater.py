#!/usr/bin/env python3
"""
New-API Channel Proxy Updater
一个定时任务，用于定期更新指定渠道的代理IP

代理来源：https://github.com/TopChina/proxy-list/blob/main/README.md

代理测试说明：
- 代理每小时更新一次用户名，密码默认为1
- 并发测试候选代理，使用带认证的代理 URL
- 使用 TTFB（首字节时间）衡量延迟，避免下载正文影响
- 多 URL × 多次采样，按成功样本去极值后取平均

环境变量：
- BASE_URL: New-API实例的URL
- ADMIN_ID: 管理员用户ID
- ADMIN_TOKEN: 管理员访问令牌
- CHANNEL_IDS: 需要更新代理的渠道ID列表
- PROXY_REGION: 代理地区 (默认: 香港)
- MAX_PROXY_TEST_COUNT: 最大代理测试数量 (默认: 5)
- MIN_SUCCESS_RATE: 最低成功率要求 (默认: 0.8)
- MAX_LATENCY_MS: 最大可接受延迟 (默认: 5000ms)
- TEST_COUNT: 每个 URL 的采样次数 (默认: 3)
- TEST_TIMEOUT: 单次请求超时秒数 (默认: 10)
- TEST_CONCURRENCY: 并发工作线程数 (默认: 5)
- TEST_URLS: 自定义测试 URL（逗号分隔，可留空使用默认204端点）
"""

import os
import json
import re
import requests
import logging
import sys
import warnings
import hashlib
import statistics
import math
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

# 禁用 urllib3 的 InsecureRequestWarning 警告
warnings.filterwarnings('ignore', message='Unverified HTTPS request')
warnings.filterwarnings('ignore', category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('proxy_updater.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 缓存文件路径
PROXY_CACHE_FILE = 'proxy_cache.json'


class TestConfig:
    """
    测试配置管理类 - 管理代理延迟测试的各种参数配置
    """
    
    def __init__(self):
        # 测试配置参数
        self.latency_test_samples = int(os.getenv('LATENCY_TEST_SAMPLES', '5'))
        self.latency_test_urls_count = int(os.getenv('LATENCY_TEST_URLS_COUNT', '3'))
        self.latency_outlier_threshold = float(os.getenv('LATENCY_OUTLIER_THRESHOLD', '2.0'))
        self.latency_spike_threshold_ms = int(os.getenv('LATENCY_SPIKE_THRESHOLD_MS', '1000'))
        self.latency_consistency_window = float(os.getenv('LATENCY_CONSISTENCY_WINDOW', '0.3'))
        
        # 评分权重配置
        self.weight_performance = float(os.getenv('WEIGHT_PERFORMANCE', '0.4'))
        self.weight_stability = float(os.getenv('WEIGHT_STABILITY', '0.35'))
        self.weight_availability = float(os.getenv('WEIGHT_AVAILABILITY', '0.25'))
        
        # 原有配置保持兼容
        self.max_test_count = int(os.getenv('MAX_PROXY_TEST_COUNT', '5'))
        self.min_success_rate = float(os.getenv('MIN_SUCCESS_RATE', '0.8'))
        self.max_latency_ms = int(os.getenv('MAX_LATENCY_MS', '5000'))
        self.test_count = int(os.getenv('TEST_COUNT', '3'))
        self.timeout = int(os.getenv('TEST_TIMEOUT', '10'))
        self.concurrency = int(os.getenv('TEST_CONCURRENCY', '5'))
    
    def get_test_urls(self, category: str = 'mixed') -> List[str]:
        """
        根据测试类别获取测试URL列表
        
        Args:
            category: 测试类别 ('fast', 'standard', 'heavy', 'mixed')
            
        Returns:
            List[str]: 测试URL列表
        """
        # 允许用户自定义测试URL
        custom_test_urls = os.getenv('TEST_URLS')
        if custom_test_urls:
            return [u.strip() for u in custom_test_urls.split(',') if u.strip()]
        
        # 快速响应类URL (主要用于连通性测试)
        fast_urls = [
            "http://www.gstatic.com/generate_204",
            "https://www.gstatic.com/generate_204",
            "http://cp.cloudflare.com/generate_204",
            "http://connectivitycheck.gstatic.com/generate_204",
        ]
        
        # 标准API类URL (模拟真实API请求)
        standard_urls = [
            "https://httpbin.org/status/200",
            "https://jsonplaceholder.typicode.com/posts/1",
            "https://api.github.com/zen",
            "https://httpstat.us/200",
        ]
        
        # 重负载类URL (测试稳定性)
        heavy_urls = [
            "https://httpbin.org/delay/1",
            "https://httpbin.org/bytes/1024",
            "https://www.google.com/favicon.ico",
        ]
        
        if category == 'fast':
            return fast_urls[:self.latency_test_urls_count]
        elif category == 'standard':
            return standard_urls[:self.latency_test_urls_count]
        elif category == 'heavy':
            return heavy_urls[:self.latency_test_urls_count]
        else:  # mixed
            # 混合模式：快速响应为主，适当加入标准API测试
            mixed_urls = fast_urls[:2] + standard_urls[:1]
            return mixed_urls[:self.latency_test_urls_count]


class ErrorHandler:
    """
    错误处理和降级策略管理类
    """
    
    @staticmethod
    def is_recoverable_error(error_type: str) -> bool:
        """
        判断错误是否可恢复
        
        Args:
            error_type: 错误类型
            
        Returns:
            bool: 是否可恢复
        """
        recoverable_errors = {
            'Timeout', 'Connection Error', 'HTTP 502', 'HTTP 503', 'HTTP 504'
        }
        return error_type in recoverable_errors
    
    @staticmethod
    def should_use_fallback_strategy(successful_samples: int, total_samples: int) -> bool:
        """
        判断是否应该使用降级策略
        
        Args:
            successful_samples: 成功样本数
            total_samples: 总样本数
            
        Returns:
            bool: 是否使用降级策略
        """
        if total_samples == 0:
            return True
        
        success_rate = successful_samples / total_samples
        min_samples = 3
        min_success_rate = 0.3
        
        return successful_samples < min_samples or success_rate < min_success_rate
    
    @staticmethod
    def get_fallback_urls() -> List[str]:
        """
        获取降级测试URL（最可靠的URL）
        
        Returns:
            List[str]: 降级测试URL列表
        """
        return [
            "http://www.gstatic.com/generate_204",
            "https://www.gstatic.com/generate_204"
        ]


class LatencyStatistics:
    """
    延迟统计计算器 - 提供全面的延迟分析指标
    
    支持计算基础统计、变异性指标、稳健性指标和API场景特定指标
    """
    
    @staticmethod
    def calculate_basic_stats(latencies: List[float]) -> Dict[str, Optional[float]]:
        """
        计算基础统计指标
        
        Args:
            latencies: 延迟数据列表 (毫秒)
            
        Returns:
            Dict: 包含基础统计指标的字典
        """
        if not latencies:
            return {
                'mean_latency_ms': None,
                'median_latency_ms': None,
                'min_latency_ms': None,
                'max_latency_ms': None,
                'p25_latency_ms': None,
                'p75_latency_ms': None,
                'p95_latency_ms': None,
                'p99_latency_ms': None
            }
        
        sorted_latencies = sorted(latencies)
        
        # 基础统计指标
        mean_latency = statistics.mean(latencies)
        median_latency = statistics.median(latencies)
        min_latency = min(latencies)
        max_latency = max(latencies)
        
        # 计算分位数
        p25 = LatencyStatistics._percentile(sorted_latencies, 25)
        p75 = LatencyStatistics._percentile(sorted_latencies, 75)
        p95 = LatencyStatistics._percentile(sorted_latencies, 95)
        p99 = LatencyStatistics._percentile(sorted_latencies, 99)
        
        return {
            'mean_latency_ms': mean_latency,
            'median_latency_ms': median_latency,
            'min_latency_ms': min_latency,
            'max_latency_ms': max_latency,
            'p25_latency_ms': p25,
            'p75_latency_ms': p75,
            'p95_latency_ms': p95,
            'p99_latency_ms': p99
        }
    
    @staticmethod
    def calculate_variability_stats(latencies: List[float]) -> Dict[str, Optional[float]]:
        """
        计算变异性指标
        
        Args:
            latencies: 延迟数据列表 (毫秒)
            
        Returns:
            Dict: 包含变异性指标的字典
        """
        if not latencies or len(latencies) < 2:
            return {
                'std_dev_ms': None,
                'coefficient_variation': None,
                'iqr_ms': None,
                'robust_std_dev_ms': None,
                'mad_ms': None
            }
        
        # 基础变异性指标
        mean_latency = statistics.mean(latencies)
        std_dev = statistics.stdev(latencies)
        coefficient_variation = std_dev / mean_latency if mean_latency > 0 else None
        
        # 稳健变异性指标
        sorted_latencies = sorted(latencies)
        p25 = LatencyStatistics._percentile(sorted_latencies, 25)
        p75 = LatencyStatistics._percentile(sorted_latencies, 75)
        iqr = p75 - p25 if p25 is not None and p75 is not None else None
        
        # 中位数绝对偏差 (MAD)
        median_latency = statistics.median(latencies)
        mad = statistics.median([abs(x - median_latency) for x in latencies])
        
        # 稳健标准差 (基于MAD)
        robust_std_dev = mad * 1.4826 if mad is not None else None
        
        return {
            'std_dev_ms': std_dev,
            'coefficient_variation': coefficient_variation,
            'iqr_ms': iqr,
            'robust_std_dev_ms': robust_std_dev,
            'mad_ms': mad
        }
    
    @staticmethod
    def calculate_robustness_stats(latencies: List[float]) -> Dict[str, Optional[float]]:
        """
        计算稳健性指标
        
        Args:
            latencies: 延迟数据列表 (毫秒)
            
        Returns:
            Dict: 包含稳健性指标的字典
        """
        if not latencies:
            return {
                'consistency_score': None,
                'stability_index': None,
                'outlier_ratio': None,
                'trimmed_mean_ms': None
            }
        
        # 一致性评分 (基于变异系数的倒数)
        mean_latency = statistics.mean(latencies)
        if len(latencies) >= 2 and mean_latency > 0:
            std_dev = statistics.stdev(latencies)
            cv = std_dev / mean_latency
            consistency_score = 1 / (1 + cv) if cv >= 0 else 0
        else:
            consistency_score = 1.0
        
        # 稳定性指数 (基于IQR的归一化)
        sorted_latencies = sorted(latencies)
        p25 = LatencyStatistics._percentile(sorted_latencies, 25)
        p75 = LatencyStatistics._percentile(sorted_latencies, 75)
        if p25 is not None and p75 is not None and mean_latency > 0:
            iqr = p75 - p25
            stability_index = 1 / (1 + iqr / mean_latency)
        else:
            stability_index = 1.0
        
        # 异常值比例 (使用1.5倍IQR规则)
        outlier_count = 0
        if p25 is not None and p75 is not None:
            iqr = p75 - p25
            lower_bound = p25 - 1.5 * iqr
            upper_bound = p75 + 1.5 * iqr
            outlier_count = sum(1 for x in latencies if x < lower_bound or x > upper_bound)
        outlier_ratio = outlier_count / len(latencies) if latencies else 0
        
        # 去极值平均数 (10%截尾平均)
        trimmed_mean = LatencyStatistics._trimmed_mean(latencies, 0.1)
        
        return {
            'consistency_score': consistency_score,
            'stability_index': stability_index,
            'outlier_ratio': outlier_ratio,
            'trimmed_mean_ms': trimmed_mean
        }
    
    @staticmethod
    def calculate_api_performance_stats(
        latencies: List[float], 
        success_rate: float,
        spike_threshold_ms: float = 1000.0,
        timeout_threshold_ms: float = 10000.0
    ) -> Dict[str, Optional[float]]:
        """
        计算API场景特定指标
        
        Args:
            latencies: 延迟数据列表 (毫秒)
            success_rate: 成功率 (0-1)
            spike_threshold_ms: 突发延迟阈值 (毫秒)
            timeout_threshold_ms: 超时阈值 (毫秒)
            
        Returns:
            Dict: 包含API性能指标的字典
        """
        if not latencies:
            return {
                'spike_rate': None,
                'timeout_risk_score': None,
                'availability_stability': success_rate,
                'qos_score': None,
                'sustained_performance_score': None
            }
        
        # 突发延迟率
        spike_count = sum(1 for x in latencies if x > spike_threshold_ms)
        spike_rate = spike_count / len(latencies)
        
        # 超时风险评分 (基于P95延迟)
        sorted_latencies = sorted(latencies)
        p95 = LatencyStatistics._percentile(sorted_latencies, 95)
        if p95 is not None:
            timeout_risk_score = min(1.0, p95 / timeout_threshold_ms)
        else:
            timeout_risk_score = 0.0
        
        # 可用性稳定性 (就是成功率)
        availability_stability = success_rate
        
        # QoS综合评分 (考虑延迟、稳定性和可用性)
        mean_latency = statistics.mean(latencies)
        # 延迟性能评分 (越低越好，归一化到0-1)
        latency_score = max(0, 1 - mean_latency / 5000)  # 5秒作为基准
        
        # 稳定性评分 (基于变异系数)
        if len(latencies) >= 2:
            std_dev = statistics.stdev(latencies)
            cv = std_dev / mean_latency if mean_latency > 0 else 0
            stability_score = 1 / (1 + cv)
        else:
            stability_score = 1.0
        
        # QoS综合评分 (权重：延迟40%，稳定性35%，可用性25%)
        qos_score = (
            latency_score * 0.4 + 
            stability_score * 0.35 + 
            availability_stability * 0.25
        )
        
        # 持续性能评分 (结合多个维度)
        sustained_performance_score = (
            (1 - spike_rate) * 0.3 +
            (1 - timeout_risk_score) * 0.3 +
            stability_score * 0.4
        )
        
        return {
            'spike_rate': spike_rate,
            'timeout_risk_score': timeout_risk_score,
            'availability_stability': availability_stability,
            'qos_score': qos_score,
            'sustained_performance_score': sustained_performance_score
        }
    
    @staticmethod
    def _percentile(sorted_data: List[float], percentile: float) -> Optional[float]:
        """
        计算百分位数
        
        Args:
            sorted_data: 已排序的数据
            percentile: 百分位数 (0-100)
            
        Returns:
            Optional[float]: 百分位数值
        """
        if not sorted_data:
            return None
        
        if percentile <= 0:
            return sorted_data[0]
        if percentile >= 100:
            return sorted_data[-1]
        
        index = (len(sorted_data) - 1) * percentile / 100
        lower_index = int(math.floor(index))
        upper_index = int(math.ceil(index))
        
        if lower_index == upper_index:
            return sorted_data[lower_index]
        
        # 线性插值
        weight = index - lower_index
        return sorted_data[lower_index] * (1 - weight) + sorted_data[upper_index] * weight
    
    @staticmethod
    def _trimmed_mean(data: List[float], trim_ratio: float) -> Optional[float]:
        """
        计算截尾平均数
        
        Args:
            data: 数据列表
            trim_ratio: 截尾比例 (0-0.5)
            
        Returns:
            Optional[float]: 截尾平均数
        """
        if not data:
            return None
        
        if trim_ratio <= 0:
            return statistics.mean(data)
        
        sorted_data = sorted(data)
        n = len(sorted_data)
        trim_count = int(n * trim_ratio)
        
        if trim_count * 2 >= n:
            return statistics.median(data)
        
        trimmed_data = sorted_data[trim_count:n-trim_count]
        return statistics.mean(trimmed_data) if trimmed_data else None


class ProxyEvaluator:
    """
    代理评估器 - 提供综合代理质量评估和排序功能
    
    支持多维度评分和场景适应性调整
    """
    
    def __init__(self, weights: Optional[Dict[str, float]] = None):
        """
        初始化代理评估器
        
        Args:
            weights: 评分权重配置
        """
        # 默认权重配置
        default_weights = {
            'performance': 0.4,     # 性能权重 40%
            'stability': 0.35,      # 稳定性权重 35%
            'availability': 0.25    # 可用性权重 25%
        }
        
        self.weights = weights if weights else default_weights
        
        # 子指标权重配置
        self.performance_sub_weights = {
            'mean_latency': 0.5,
            'p95_latency': 0.3,
            'median_latency': 0.2
        }
        
        self.stability_sub_weights = {
            'coefficient_variation': 0.4,
            'consistency_score': 0.35,
            'outlier_ratio': 0.25
        }
        
        self.availability_sub_weights = {
            'success_rate': 0.6,
            'connection_stability': 0.25,
            'timeout_risk': 0.15
        }
    
    def calculate_performance_score(self, basic_stats: Dict, variability_stats: Dict) -> float:
        """
        计算性能评分
        
        Args:
            basic_stats: 基础统计指标
            variability_stats: 变异性指标
            
        Returns:
            float: 性能评分 (0-1)
        """
        mean_latency = basic_stats.get('mean_latency_ms', float('inf'))
        p95_latency = basic_stats.get('p95_latency_ms', float('inf'))
        median_latency = basic_stats.get('median_latency_ms', float('inf'))
        
        # 将延迟转换为评分 (0-1，越低越好)
        mean_score = max(0, 1 - mean_latency / 5000) if mean_latency != float('inf') else 0
        p95_score = max(0, 1 - p95_latency / 8000) if p95_latency != float('inf') else 0
        median_score = max(0, 1 - median_latency / 4000) if median_latency != float('inf') else 0
        
        # 加权平均
        performance_score = (
            mean_score * self.performance_sub_weights['mean_latency'] +
            p95_score * self.performance_sub_weights['p95_latency'] +
            median_score * self.performance_sub_weights['median_latency']
        )
        
        return min(1.0, max(0.0, performance_score))
    
    def calculate_stability_score(self, variability_stats: Dict, robustness_stats: Dict) -> float:
        """
        计算稳定性评分
        
        Args:
            variability_stats: 变异性指标
            robustness_stats: 稳健性指标
            
        Returns:
            float: 稳定性评分 (0-1)
        """
        cv = variability_stats.get('coefficient_variation', float('inf'))
        consistency_score = robustness_stats.get('consistency_score', 0)
        outlier_ratio = robustness_stats.get('outlier_ratio', 1)
        
        # 变异系数评分 (越低越好)
        cv_score = 1 / (1 + cv) if cv != float('inf') else 0
        
        # 一致性评分 (已经是0-1范围)
        consistency = consistency_score if consistency_score is not None else 0
        
        # 异常值评分 (越低越好)
        outlier_score = 1 - outlier_ratio
        
        # 加权平均
        stability_score = (
            cv_score * self.stability_sub_weights['coefficient_variation'] +
            consistency * self.stability_sub_weights['consistency_score'] +
            outlier_score * self.stability_sub_weights['outlier_ratio']
        )
        
        return min(1.0, max(0.0, stability_score))
    
    def calculate_availability_score(
        self, 
        success_rate: float, 
        api_performance_stats: Dict
    ) -> float:
        """
        计算可用性评分
        
        Args:
            success_rate: 成功率 (0-1)
            api_performance_stats: API性能指标
            
        Returns:
            float: 可用性评分 (0-1)
        """
        # 成功率评分
        success_score = success_rate
        
        # 连接稳定性 (基于可用性稳定性)
        connection_stability = api_performance_stats.get('availability_stability', success_rate)
        
        # 超时风险评分 (越低越好)
        timeout_risk = api_performance_stats.get('timeout_risk_score', 0)
        timeout_score = 1 - timeout_risk
        
        # 加权平均
        availability_score = (
            success_score * self.availability_sub_weights['success_rate'] +
            connection_stability * self.availability_sub_weights['connection_stability'] +
            timeout_score * self.availability_sub_weights['timeout_risk']
        )
        
        return min(1.0, max(0.0, availability_score))
    
    def calculate_composite_score(self, stats: Dict) -> float:
        """
        计算综合评分
        
        Args:
            stats: 完整的统计数据
            
        Returns:
            float: 综合评分 (0-1)
        """
        basic_stats = stats.get('basic_stats', {})
        variability_stats = stats.get('variability_stats', {})
        robustness_stats = stats.get('robustness_stats', {})
        api_performance_stats = stats.get('api_performance', {})
        success_rate = stats.get('success_rate', 0)
        
        # 计算各维度评分
        performance_score = self.calculate_performance_score(basic_stats, variability_stats)
        stability_score = self.calculate_stability_score(variability_stats, robustness_stats)
        availability_score = self.calculate_availability_score(success_rate, api_performance_stats)
        
        # 综合评分
        composite_score = (
            performance_score * self.weights['performance'] +
            stability_score * self.weights['stability'] +
            availability_score * self.weights['availability']
        )
        
        return min(1.0, max(0.0, composite_score))
    
    def calculate_qos_score(self, stats: Dict) -> float:
        """
        计算QoS评分 (从 API 性能指标中直接获取或计算)
        
        Args:
            stats: 完整的统计数据
            
        Returns:
            float: QoS评分 (0-1)
        """
        api_performance = stats.get('api_performance', {})
        qos_score = api_performance.get('qos_score')
        
        if qos_score is not None:
            return qos_score
        
        # 如果没有直接的QoS评分，使用综合评分
        return self.calculate_composite_score(stats)
    
    def rank_proxies(self, proxy_results: List[Dict]) -> List[Dict]:
        """
        对代理列表进行排序
        
        Args:
            proxy_results: 代理测试结果列表
            
        Returns:
            List[Dict]: 排序后的代理列表
        """
        # 为每个代理计算综合评分
        for proxy in proxy_results:
            if proxy.get('is_working') and proxy.get('enhanced_stats'):
                proxy['composite_score'] = self.calculate_composite_score(proxy['enhanced_stats'])
                proxy['qos_score'] = self.calculate_qos_score(proxy['enhanced_stats'])
            else:
                proxy['composite_score'] = 0.0
                proxy['qos_score'] = 0.0
        
        # 按综合评分降序排列
        ranked_proxies = sorted(
            proxy_results,
            key=lambda x: (
                x.get('is_working', False),  # 首先按是否可用
                x.get('composite_score', 0)  # 其次按综合评分
            ),
            reverse=True
        )
        
        return ranked_proxies
    
    def select_best_proxy(
        self, 
        proxy_results: List[Dict],
        min_composite_score: float = 0.6,
        min_qos_score: float = 0.5
    ) -> Optional[Dict]:
        """
        选择最佳代理
        
        Args:
            proxy_results: 代理测试结果列表
            min_composite_score: 最低综合评分要求
            min_qos_score: 最低QoS评分要求
            
        Returns:
            Optional[Dict]: 最佳代理，没有符合条件的返回None
        """
        ranked_proxies = self.rank_proxies(proxy_results)
        
        for proxy in ranked_proxies:
            if (
                proxy.get('is_working') and
                proxy.get('composite_score', 0) >= min_composite_score and
                proxy.get('qos_score', 0) >= min_qos_score
            ):
                return proxy
        
        # 如果没有代理满足严格条件，返回评分最高的可用代理
        for proxy in ranked_proxies:
            if proxy.get('is_working'):
                logger.warning(
                    f"没有代理达到严格标准（综合评分>={min_composite_score}, QoS>={min_qos_score}），"
                    f"选择评分最高的可用代理: {proxy.get('proxy')}"
                )
                return proxy
        
        return None


class EnhancedLatencyTester:
    """
    增强的延迟测试器 - 集成统计分析和评估功能
    """

    def __init__(self, test_config: Optional[TestConfig] = None):
        """
        初始化增强的延迟测试器
        
        Args:
            test_config: 测试配置对象
        """
        self.config = test_config if test_config else TestConfig()
        self.evaluator = ProxyEvaluator({
            'performance': self.config.weight_performance,
            'stability': self.config.weight_stability,
            'availability': self.config.weight_availability
        })
        self.error_handler = ErrorHandler()
    
    def measure_proxy_latency_enhanced(
        self,
        proxy_ip: str,
        proxy_port: str,
        proxy_user: str,
        proxy_password: str = "1",
        test_urls: Optional[List[str]] = None,
        test_count: Optional[int] = None,
        timeout: Optional[int] = None,
        use_fallback: bool = True
    ) -> Dict:
        """
        增强的代理延迟测试
        
        Args:
            proxy_ip: 代理IP地址
            proxy_port: 代理端口
            proxy_user: 代理用户名
            proxy_password: 代理密码
            test_urls: 测试URL列表
            test_count: 测试次数
            timeout: 超时时间
            use_fallback: 是否使用降级策略
            
        Returns:
            Dict: 增强的测试结果
        """
        # 使用配置默认值
        if test_urls is None:
            test_urls = self.config.get_test_urls('mixed')
        if test_count is None:
            test_count = self.config.latency_test_samples
        if timeout is None:
            timeout = self.config.timeout
        
        all_results = []
        working_tests = 0
        test_details = []
        
        logger.debug(f"开始增强延迟测试: {proxy_ip}:{proxy_port}")
        
        # 执行测试
        for test_url in test_urls:
            url_results = []
            for i in range(test_count):
                result = test_proxy_connectivity(
                    proxy_ip, proxy_port, proxy_user, proxy_password, test_url, timeout
                )
                url_results.append(result)
                
                if result['is_working'] and result['latency_ms'] is not None:
                    working_tests += 1
                    all_results.append(result['latency_ms'])
                
                # 短暂延迟
                if i < test_count - 1:
                    time.sleep(0.2)
            
            test_details.append({
                'url': test_url,
                'results': url_results
            })
        
        # 检查是否需要降级策略
        total_tests = len(test_urls) * test_count
        if use_fallback and self.error_handler.should_use_fallback_strategy(working_tests, total_tests):
            logger.warning(f"代理 {proxy_ip}:{proxy_port} 样本不足，启用降级策略")
            return self._fallback_test(proxy_ip, proxy_port, proxy_user, proxy_password, timeout)
        
        # 没有有效数据
        if not working_tests:
            return self._create_failed_result(proxy_ip, proxy_port, proxy_user, test_details, total_tests)
        
        # 计算增强统计指标
        enhanced_stats = self._calculate_enhanced_stats(all_results, working_tests, total_tests)
        
        # 创建结果对象
        result = {
            'proxy': f"{proxy_ip}:{proxy_port}",
            'user': proxy_user,
            'is_working': True,
            
            # 原有指标保持兼容
            'avg_latency_ms': enhanced_stats['basic_stats']['mean_latency_ms'],
            'min_latency_ms': enhanced_stats['basic_stats']['min_latency_ms'],
            'max_latency_ms': enhanced_stats['basic_stats']['max_latency_ms'],
            'success_rate': enhanced_stats['success_rate'],
            'test_count': total_tests,
            'working_tests': working_tests,
            'test_details': test_details,
            
            # 新增增强统计数据
            'enhanced_stats': enhanced_stats
        }
        
        # 计算评分
        result['composite_score'] = self.evaluator.calculate_composite_score(enhanced_stats)
        result['qos_score'] = self.evaluator.calculate_qos_score(enhanced_stats)
        
        logger.debug(
            f"✅ 代理 {proxy_ip}:{proxy_port} 增强测试完成: "
            f"平均={enhanced_stats['basic_stats']['mean_latency_ms']:.1f}ms, "
            f"成功率={enhanced_stats['success_rate']:.1%}, "
            f"综合评分={result['composite_score']:.3f}"
        )
        
        return result
    
    def _calculate_enhanced_stats(self, latencies: List[float], working_tests: int, total_tests: int) -> Dict:
        """
        计算增强统计指标
        
        Args:
            latencies: 延迟数据列表
            working_tests: 成功测试次数
            total_tests: 总测试次数
            
        Returns:
            Dict: 增强统计指标
        """
        success_rate = working_tests / total_tests if total_tests > 0 else 0
        
        # 计算各类统计指标
        basic_stats = LatencyStatistics.calculate_basic_stats(latencies)
        variability_stats = LatencyStatistics.calculate_variability_stats(latencies)
        robustness_stats = LatencyStatistics.calculate_robustness_stats(latencies)
        api_performance_stats = LatencyStatistics.calculate_api_performance_stats(
            latencies, 
            success_rate,
            self.config.latency_spike_threshold_ms,
            self.config.timeout * 1000  # 转换为毫秒
        )
        
        return {
            'basic_stats': basic_stats,
            'variability_stats': variability_stats,
            'robustness_stats': robustness_stats,
            'api_performance': api_performance_stats,
            'success_rate': success_rate,
            'test_metadata': {
                'total_requests': total_tests,
                'successful_requests': working_tests,
                'sampling_strategy': 'enhanced_multi_url_weighted'
            }
        }
    
    def _fallback_test(
        self, 
        proxy_ip: str, 
        proxy_port: str, 
        proxy_user: str, 
        proxy_password: str, 
        timeout: int
    ) -> Dict:
        """
        降级测试策略
        
        Args:
            proxy_ip: 代理IP
            proxy_port: 代理端口
            proxy_user: 代理用户名
            proxy_password: 代理密码
            timeout: 超时时间
            
        Returns:
            Dict: 降级测试结果
        """
        fallback_urls = self.error_handler.get_fallback_urls()
        fallback_results = []
        working_tests = 0
        
        logger.debug(f"使用降级策略测试代理: {proxy_ip}:{proxy_port}")
        
        for url in fallback_urls:
            for _ in range(3):  # 操作3次
                result = test_proxy_connectivity(
                    proxy_ip, proxy_port, proxy_user, proxy_password, url, timeout
                )
                if result['is_working'] and result['latency_ms'] is not None:
                    working_tests += 1
                    fallback_results.append(result['latency_ms'])
                time.sleep(0.1)
        
        total_fallback_tests = len(fallback_urls) * 3
        
        if not working_tests:
            return self._create_failed_result(proxy_ip, proxy_port, proxy_user, [], total_fallback_tests)
        
        # 使用简化的统计指标
        enhanced_stats = self._calculate_enhanced_stats(fallback_results, working_tests, total_fallback_tests)
        
        result = {
            'proxy': f"{proxy_ip}:{proxy_port}",
            'user': proxy_user,
            'is_working': True,
            'avg_latency_ms': enhanced_stats['basic_stats']['mean_latency_ms'],
            'min_latency_ms': enhanced_stats['basic_stats']['min_latency_ms'],
            'max_latency_ms': enhanced_stats['basic_stats']['max_latency_ms'],
            'success_rate': enhanced_stats['success_rate'],
            'test_count': total_fallback_tests,
            'working_tests': working_tests,
            'test_details': [],
            'enhanced_stats': enhanced_stats,
            'is_fallback': True  # 标记为降级结果
        }
        
        result['composite_score'] = self.evaluator.calculate_composite_score(enhanced_stats)
        result['qos_score'] = self.evaluator.calculate_qos_score(enhanced_stats)
        
        logger.warning(
            f"⚠️ 代理 {proxy_ip}:{proxy_port} 降级测试完成: "
            f"平均={enhanced_stats['basic_stats']['mean_latency_ms']:.1f}ms, "
            f"成功率={enhanced_stats['success_rate']:.1%}"
        )
        
        return result
    
    def _create_failed_result(
        self, 
        proxy_ip: str, 
        proxy_port: str, 
        proxy_user: str, 
        test_details: List[Dict], 
        total_tests: int
    ) -> Dict:
        """
        创建失败结果
        
        Args:
            proxy_ip: 代理IP
            proxy_port: 代理端口
            proxy_user: 代理用户名
            test_details: 测试详情
            total_tests: 总测试次数
            
        Returns:
            Dict: 失败结果
        """
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 所有测试均失败")
        
        return {
            'proxy': f"{proxy_ip}:{proxy_port}",
            'user': proxy_user,
            'is_working': False,
            'avg_latency_ms': None,
            'min_latency_ms': None,
            'max_latency_ms': None,
            'success_rate': 0.0,
            'test_count': total_tests,
            'working_tests': 0,
            'test_details': test_details,
            'enhanced_stats': None,
            'composite_score': 0.0,
            'qos_score': 0.0
        }
    
    def batch_test_proxies(self, proxies: List[Tuple[str, str]]) -> List[Dict]:
        """
        批量测试代理
        
        Args:
            proxies: 代理列表 [(ip_port, user), ...]
            
        Returns:
            List[Dict]: 测试结果列表
        """
        logger.info(f"开始批量测试 {len(proxies)} 个代理")
        
        results = []
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            future_map = {}
            
            for ip_port, user in proxies:
                try:
                    proxy_ip, proxy_port = ip_port.split(":")
                    future = executor.submit(
                        self.measure_proxy_latency_enhanced,
                        proxy_ip, proxy_port, user
                    )
                    future_map[future] = (ip_port, user)
                except ValueError:
                    # 无效的 ip:port 格式
                    results.append({
                        'proxy': ip_port,
                        'user': user,
                        'is_working': False,
                        'error': 'Invalid ip:port format',
                        'composite_score': 0.0,
                        'qos_score': 0.0
                    })
            
            for future in as_completed(future_map):
                ip_port, user = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.warning(f"代理 {ip_port} 测试异常: {e}")
                    results.append({
                        'proxy': ip_port,
                        'user': user,
                        'is_working': False,
                        'error': str(e),
                        'composite_score': 0.0,
                        'qos_score': 0.0
                    })
        
        return results


def find_best_proxy_by_latency_enhanced(
    proxies: List[Tuple[str, str]],
    max_test_count: int = 5,
    min_success_rate: float = 0.8,
    max_latency_ms: int = 5000,
    min_composite_score: float = 0.6,
    min_qos_score: float = 0.5,
    test_config: Optional[TestConfig] = None
) -> Optional[Dict]:
    """
    增强版本的代理延迟测试和评估
    
    Args:
        proxies: 代理列表，每个元素为(ip_port, user)元组
        max_test_count: 最大测试数量
        min_success_rate: 最低成功率要求
        max_latency_ms: 最大可接受延迟
        min_composite_score: 最低综合评分要求
        min_qos_score: 最低QoS评分要求
        test_config: 测试配置
        
    Returns:
        Optional[Dict]: 最佳代理的详细信息，如果没有则返回None
    """
    logger.info(f"开始增强代理延迟测试，共 {len(proxies)} 个代理，最多测试 {max_test_count} 个")
    
    # 初始化增强测试器
    tester = EnhancedLatencyTester(test_config)
    evaluator = tester.evaluator
    
    # 截取候选代理
    candidates = proxies[:max_test_count]
    
    # 批量测试代理
    logger.info(f"并发测试 {len(candidates)} 个候选代理")
    test_results = tester.batch_test_proxies(candidates)
    
    # 筛选可用代理
    working_proxies = [
        proxy for proxy in test_results
        if (
            proxy.get('is_working') and
            proxy.get('success_rate', 0) >= min_success_rate and
            proxy.get('avg_latency_ms') is not None and
            proxy.get('avg_latency_ms') <= max_latency_ms
        )
    ]
    
    if not working_proxies:
        logger.warning(f"❌ 在 {len(test_results)} 个测试的代理中未找到符合条件的代理")
        return None
    
    # 使用评估器选择最佳代理
    best_proxy = evaluator.select_best_proxy(
        working_proxies,
        min_composite_score,
        min_qos_score
    )
    
    if not best_proxy:
        logger.warning(f"❌ 没有代理达到评分要求（综合>={min_composite_score}, QoS>={min_qos_score}）")
        return None
    
    # 输出详细统计信息
    logger.info(f"🎯 选择最佳代理: {best_proxy['proxy']}")
    
    # 输出基础指标
    enhanced_stats = best_proxy.get('enhanced_stats', {})
    basic_stats = enhanced_stats.get('basic_stats', {})
    api_performance = enhanced_stats.get('api_performance', {})
    
    logger.info(f"   - 平均延迟: {basic_stats.get('mean_latency_ms', 0):.1f}ms")
    logger.info(f"   - 中位数延迟: {basic_stats.get('median_latency_ms', 0):.1f}ms")
    logger.info(f"   - P95延迟: {basic_stats.get('p95_latency_ms', 0):.1f}ms")
    logger.info(f"   - 成功率: {best_proxy.get('success_rate', 0):.1%}")
    logger.info(f"   - 综合评分: {best_proxy.get('composite_score', 0):.3f}")
    logger.info(f"   - QoS评分: {best_proxy.get('qos_score', 0):.3f}")
    
    # 输出高级指标
    variability_stats = enhanced_stats.get('variability_stats', {})
    robustness_stats = enhanced_stats.get('robustness_stats', {})
    
    logger.info(f"   - 变异系数: {variability_stats.get('coefficient_variation', 0):.3f}")
    logger.info(f"   - 一致性评分: {robustness_stats.get('consistency_score', 0):.3f}")
    logger.info(f"   - 突发延迟率: {api_performance.get('spike_rate', 0):.1%}")
    logger.info(f"   - 超时风险: {api_performance.get('timeout_risk_score', 0):.3f}")
    
    # 输出测试详情
    logger.info(f"   - 测试次数: {best_proxy.get('working_tests', 0)}/{best_proxy.get('test_count', 0)}")
    
    if best_proxy.get('is_fallback'):
        logger.info(f"   - 类型: 降级测试结果")
    
    return best_proxy


def get_cached_md5() -> Optional[str]:
    """
    读取缓存的MD5值
    
    Returns:
        Optional[str]: 缓存的MD5值，如果缓存文件不存在或读取失败则返回None
    """
    try:
        if os.path.exists(PROXY_CACHE_FILE):
            with open(PROXY_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                return cache_data.get('last_md5')
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"读取缓存文件失败: {e}")
    return None


def save_cached_md5(md5_value: str) -> None:
    """
    保存新的MD5值到缓存文件
    
    Args:
        md5_value: 要保存的MD5值
    """
    try:
        cache_data = {
            'last_md5': md5_value,
            'last_updated': datetime.now().isoformat()
        }
        with open(PROXY_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        logger.debug(f"已保存MD5缓存: {md5_value}")
    except IOError as e:
        logger.warning(f"保存缓存文件失败: {e}")


def calculate_content_md5(content: str) -> str:
    """
    计算内容的MD5值
    
    Args:
        content: 要计算MD5的内容
    
    Returns:
        str: 内容的MD5值
    """
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def test_proxy_connectivity(proxy_ip: str, proxy_port: str, proxy_user: str, proxy_password: str = "1",
                          test_url: str = "http://www.gstatic.com/generate_204", timeout: int = 10) -> dict:
    """
    测试代理的连通性和延迟
    
    Args:
        proxy_ip: 代理IP地址
        proxy_port: 代理端口
        proxy_user: 代理用户名
        proxy_password: 代理密码 (默认为1)
        test_url: 测试URL (默认 http://www.gstatic.com/generate_204)
        timeout: 超时时间 (秒)
    
    Returns:
        dict: 包含连通性和延迟信息的字典
    """
    try:
        # 构建代理URL（带认证信息）
        encoded_user = quote(proxy_user)
        proxy_url = f"http://{encoded_user}:{proxy_password}@{proxy_ip}:{proxy_port}"
        
        # 设置代理
        proxies = {
            'http': proxy_url,
            'https': proxy_url
        }
        
        # 使用 User-Agent；不再手工设置 Proxy-Authorization，交给 requests 处理
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        logger.debug(f"正在测试代理: {proxy_ip}:{proxy_port}")
        
        # 发送测试请求（stream=True，仅测到首字节/首包时间）
        response = requests.get(
            test_url,
            proxies=proxies,
            headers=headers,
            timeout=timeout,
            verify=False,
            stream=True,
            allow_redirects=False
        )
        latency_ms = response.elapsed.total_seconds() * 1000 if response.elapsed else None
        # 关闭连接，避免下载正文
        response.close()

        if 200 <= response.status_code < 400 and latency_ms is not None:
            logger.debug(f"✅ 代理 {proxy_ip}:{proxy_port} 测试成功，TTFB: {latency_ms:.1f}ms")
            return {
                'is_working': True,
                'latency_ms': latency_ms,
                'status_code': response.status_code,
                'test_url': test_url,
                'error': None
            }
        else:
            logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 测试失败: HTTP {response.status_code}")
            return {
                'is_working': False,
                'latency_ms': latency_ms,
                'status_code': response.status_code,
                'test_url': test_url,
                'error': f"HTTP {response.status_code}"
            }
            
    except requests.exceptions.Timeout:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 测试超时")
        return {
            'is_working': False,
            'latency_ms': None,
            'status_code': None,
            'test_url': test_url,
            'error': "Timeout"
        }
    except requests.exceptions.ProxyError:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 代理错误")
        return {
            'is_working': False,
            'latency_ms': None,
            'status_code': None,
            'test_url': test_url,
            'error': "Proxy Error"
        }
    except requests.exceptions.ConnectionError:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 连接错误")
        return {
            'is_working': False,
            'latency_ms': None,
            'status_code': None,
            'test_url': test_url,
            'error': "Connection Error"
        }
    except Exception as e:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 测试异常: {str(e)}")
        return {
            'is_working': False,
            'latency_ms': None,
            'status_code': None,
            'test_url': test_url,
            'error': str(e)
        }


def measure_proxy_latency(
    proxy_ip: str,
    proxy_port: str,
    proxy_user: str,
    proxy_password: str = "1",
    test_urls: List[str] = None,
    test_count: int = 3,
    timeout: int = 10,
) -> dict:
    """
    精确测量代理延迟，使用多个URL和多次测试取平均值
    
    Args:
        proxy_ip: 代理IP地址
        proxy_port: 代理端口
        proxy_user: 代理用户名
        proxy_password: 代理密码 (默认为1)
        test_urls: 测试URL列表 (默认使用内置的快速响应站点)
        test_count: 每个URL测试次数 (默认为3)
        timeout: 超时时间 (秒)
    
    Returns:
        dict: 包含详细延迟信息的字典
    """
    if test_urls is None:
        # 选择极小响应或 204 的探活端点，减少下载时间影响
        test_urls = [
            "http://www.gstatic.com/generate_204",
            "https://www.gstatic.com/generate_204",
            "http://cp.cloudflare.com/generate_204",
        ]
    
    all_results = []
    working_tests = 0
    
    logger.debug(f"开始精确测量代理延迟: {proxy_ip}:{proxy_port}")
    
    test_details: List[Dict[str, Any]] = []

    for test_url in test_urls:
        url_results: List[Dict[str, Any]] = []

        for i in range(test_count):
            result = test_proxy_connectivity(
                proxy_ip, proxy_port, proxy_user, proxy_password, test_url, timeout
            )
            url_results.append(result)

            if result['is_working'] and result['latency_ms'] is not None:
                working_tests += 1
                all_results.append(result['latency_ms'])

            # 添加短暂延迟避免频繁请求
            if i < test_count - 1:
                time.sleep(0.2)

        test_details.append({
            'url': test_url,
            'results': url_results,
        })
    
    if not working_tests:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 所有测试均失败")
        return {
            'proxy': f"{proxy_ip}:{proxy_port}",
            'user': proxy_user,
            'is_working': False,
            'avg_latency_ms': None,
            'min_latency_ms': None,
            'max_latency_ms': None,
            'success_rate': 0.0,
            'test_count': len(test_urls) * test_count,
            'working_tests': 0,
            'test_details': test_details,
        }
    
    # 去掉最高和最低延迟后计算平均值
    valid_latencies = [latency for latency in all_results if latency is not None]
    filtered_latencies: List[float] = []
    if len(valid_latencies) >= 5:
        valid_latencies.sort()
        trim = max(1, int(len(valid_latencies) * 0.1))
        filtered_latencies = valid_latencies[trim: len(valid_latencies) - trim]
    else:
        filtered_latencies = valid_latencies

    avg_latency = (
        sum(filtered_latencies) / len(filtered_latencies)
        if filtered_latencies else None
    )
    min_latency = min(valid_latencies) if valid_latencies else None
    max_latency = max(valid_latencies) if valid_latencies else None
    success_rate = working_tests / (len(test_urls) * test_count)
    
    if avg_latency is not None:
        logger.debug(
            f"✅ 代理 {proxy_ip}:{proxy_port} 延迟测试完成: 平均={avg_latency:.1f}ms, 成功率={success_rate:.1%}"
        )
    else:
        logger.debug(
            f"⚠️ 代理 {proxy_ip}:{proxy_port} 延迟测试完成: 无有效样本, 成功率={success_rate:.1%}"
        )
    
    return {
        'proxy': f"{proxy_ip}:{proxy_port}",
        'user': proxy_user,
        'is_working': True,
        'avg_latency_ms': avg_latency,
        'min_latency_ms': min_latency,
        'max_latency_ms': max_latency,
        'success_rate': success_rate,
        'test_count': len(test_urls) * test_count,
        'working_tests': working_tests,
        'test_details': test_details
    }


def _measure_single_proxy(
    ip_port: str,
    user: str,
    test_urls: Optional[List[str]],
    test_count: int,
    timeout: int,
) -> dict:
    try:
        proxy_ip, proxy_port = ip_port.split(":")
    except ValueError:
        return {
            'proxy': ip_port,
            'user': user,
            'is_working': False,
            'avg_latency_ms': None,
            'min_latency_ms': None,
            'max_latency_ms': None,
            'success_rate': 0.0,
            'test_count': 0,
            'working_tests': 0,
            'test_details': [],
            'error': 'Invalid ip:port'
        }

    return measure_proxy_latency(
        proxy_ip, proxy_port, user,
        test_urls=test_urls,
        test_count=test_count,
        timeout=timeout,
    )


def find_best_proxy_by_latency(
    proxies: List[Tuple[str, str]],
    max_test_count: int = 5,
    min_success_rate: float = 0.8,
    max_latency_ms: int = 5000,
    test_urls: Optional[List[str]] = None,
    test_count: int = 3,
    timeout: int = 10,
    concurrency: int = 5,
) -> Optional[dict]:
    """
    遍历代理列表，找到延迟最低的可用代理
    
    Args:
        proxies: 代理列表，每个元素为(ip_port, user)元组
        max_test_count: 最大测试数量 (默认为5)
        min_success_rate: 最低成功率要求 (默认为0.8)
        max_latency_ms: 最大可接受延迟 (默认为5000ms)
    
    Returns:
        Optional[dict]: 最佳代理的详细信息，如果没有则返回None
    """
    logger.info(f"开始测试代理延迟，共 {len(proxies)} 个代理，最多测试 {max_test_count} 个")
    
    tested_proxies = []

    # 截取到最多 max_test_count 个候选
    candidates = proxies[:max_test_count]

    logger.info(f"并发测试 {len(candidates)} 个候选代理，最大并发={concurrency}")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        future_map = {
            executor.submit(
                _measure_single_proxy,
                ip_port,
                user,
                test_urls,
                test_count,
                timeout,
            ): (ip_port, user)
            for (ip_port, user) in candidates
        }

        for future in as_completed(future_map):
            ip_port, user = future_map[future]
            try:
                result = future.result()
            except Exception as e:
                logger.warning(f"代理 {ip_port} 测试异常: {e}")
                result = {
                    'proxy': ip_port,
                    'user': user,
                    'is_working': False,
                    'avg_latency_ms': None,
                    'min_latency_ms': None,
                    'max_latency_ms': None,
                    'success_rate': 0.0,
                    'test_count': 0,
                    'working_tests': 0,
                    'test_details': [],
                    'error': str(e)
                }

            tested_proxies.append(result)
            if result['is_working'] and result['avg_latency_ms'] is not None:
                logger.info(
                    f"✅ 代理 {ip_port} 测试完成: 平均延迟={result['avg_latency_ms']:.1f}ms, 成功率={result['success_rate']:.1%}"
                )
            else:
                logger.info(f"❌ 代理 {ip_port} 不可用")
    
    # 筛选可用代理
    working_proxies = [
        proxy for proxy in tested_proxies
        if (
            proxy.get('is_working')
            and proxy.get('success_rate', 0) >= min_success_rate
            and proxy.get('avg_latency_ms') is not None
            and proxy.get('avg_latency_ms') <= max_latency_ms
        )
    ]
    
    if not working_proxies:
        logger.warning(f"❌ 在 {len(tested_proxies)} 个测试的代理中未找到符合条件的代理")
        return None
    
    # 按延迟排序，选择最佳代理
    best_proxy = min(working_proxies, key=lambda x: x['avg_latency_ms'])
    
    logger.info(f"🎯 选择最佳代理: {best_proxy['proxy']}")
    logger.info(f"   - 平均延迟: {best_proxy['avg_latency_ms']:.1f}ms")
    logger.info(f"   - 最小延迟: {best_proxy['min_latency_ms']:.1f}ms")
    logger.info(f"   - 最大延迟: {best_proxy['max_latency_ms']:.1f}ms")
    logger.info(f"   - 成功率: {best_proxy['success_rate']:.1%}")
    logger.info(f"   - 测试次数: {best_proxy['working_tests']}/{best_proxy['test_count']}")
    
    return best_proxy


def extract_proxies_by_region(markdown_text: str, region: str) -> List[Tuple[str, str]]:
    """
    从Markdown文本中提取指定地区的代理
    
    Args:
        markdown_text: 包含代理列表的Markdown文本
        region: 目标地区
    
    Returns:
        包含(ip_port, user)元组的列表
    """
    proxies: List[Tuple[str, str]] = []
    lines = markdown_text.split('\n')
    ip_port_regex = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$')
    
    for line in lines:
        # 检查是否为表格数据行
        if line.startswith('|') and not line.startswith('|---'):
            # 分割列并去除首尾空格
            columns = [col.strip() for col in line.split('|') if col.strip()]
            
            if len(columns) >= 3:
                ip_port = columns[0]
                country = columns[1]
                user = columns[2]
                
                # 筛选指定地区且IP:端口格式正确的代理
                if country == region and ip_port_regex.match(ip_port):
                    proxies.append((ip_port, user))
    
    return proxies


def update_channel_proxy(base_url: str, admin_id: str, admin_token: str, channel_ids: List[int], proxy_url: str) -> None:
    """
    更新指定渠道的代理设置
    
    Args:
        base_url: New-API基础URL
        admin_id: 管理员ID
        admin_token: 管理员令牌
        channel_ids: 渠道ID列表
        proxy_url: 新的代理URL
    """
    api_url = f"{base_url.rstrip('/')}/api/channel/"
    
    logger.info(f"正在更新 {len(channel_ids)} 个渠道的代理...")
    logger.debug(f"updateChannelProxy: proxyUrl={proxy_url}")
    logger.debug(f"updateChannelProxy: channelIds={channel_ids}")
    logger.debug(f"updateChannelProxy: baseUrl={base_url}")
    
    headers = {
        'New-Api-User': admin_id,
        'Authorization': f'Bearer {admin_token}',
        'Content-Type': 'application/json',
    }
    
    for channel_id in channel_ids:
        update_data = {
            'id': channel_id,
            'setting': json.dumps({'proxy': proxy_url}),
        }
        
        logger.info(f"正在更新渠道 {channel_id} 的代理...")
        logger.debug(f"updateChannelProxy: updateData={json.dumps(update_data)}")
        logger.debug(f"updateChannelProxy: headers.New-Api-User={admin_id}")
        logger.debug(f"updateChannelProxy: headers.Authorization=Bearer {admin_token[:10]}...")
        logger.debug(f"updateChannelProxy: body={json.dumps(update_data)}")
        
        try:
            response = requests.put(
                api_url,
                headers=headers,
                json=update_data,
                timeout=30
            )
            
            if response.ok:
                logger.info(f"✅ 渠道 {channel_id} 代理更新成功！")
            else:
                error_text = response.text
                logger.error(f"❌ 渠道 {channel_id} 代理更新失败：HTTP {response.status_code}: {error_text}")
                raise Exception(f"代理更新失败：HTTP {response.status_code}: {error_text}")
                
        except requests.RequestException as e:
            logger.error(f"❌ 更新渠道 {channel_id} 时发生网络错误：{str(e)}")
            raise


def main():
    """主函数 - 执行代理更新任务"""
    logger.info("开始执行代理更新任务...")
    
    # 步骤1: 检查环境变量
    required_env_vars = {
        'BASE_URL': os.getenv('BASE_URL'),
        'ADMIN_ID': os.getenv('ADMIN_ID'),
        'ADMIN_TOKEN': os.getenv('ADMIN_TOKEN'),
        'CHANNEL_IDS': os.getenv('CHANNEL_IDS')
    }
    
    missing_vars = [var for var, value in required_env_vars.items() if not value]
    if missing_vars:
        logger.error(f"错误：缺少必要的环境变量: {', '.join(missing_vars)}")
        sys.exit(1)
    
    try:
        # 解析渠道ID
        channel_ids = json.loads(required_env_vars['CHANNEL_IDS'])
        if not isinstance(channel_ids, list):
            raise ValueError("CHANNEL_IDS 必须是数组格式")
        
        # 步骤2: 从GitHub获取代理列表
        logger.info("正在从GitHub获取代理列表...")
        proxy_list_url = "https://raw.githubusercontent.com/TopChina/proxy-list/refs/heads/main/README.md"
        
        try:
            response = requests.get(proxy_list_url, timeout=30)
            response.raise_for_status()
            markdown_text = response.text
            logger.info("✅ 成功获取代理列表Markdown文件")
            
            # 检查内容MD5是否与缓存相同
            current_md5 = calculate_content_md5(markdown_text)
            cached_md5 = get_cached_md5()
            
            if cached_md5 == current_md5:
                logger.info("🔄 代理列表内容未变化，跳过本次执行")
                return
            else:
                logger.info("📝 代理列表内容有更新，继续执行")
        except requests.RequestException as e:
            logger.error(f"❌ 获取代理列表失败：{str(e)}")
            return
        
        # 步骤3: 解析代理列表
        region = os.getenv('PROXY_REGION', '香港')
        logger.info(f"正在查找 {region} 地区的代理...")
        
        proxies = extract_proxies_by_region(markdown_text, region)
        
        if not proxies:
            logger.warning(f"在列表中未找到有效的 {region} 代理。任务结束。")
            return
        
        logger.info(f"✅ 找到了 {len(proxies)} 个 {region} 代理")
        
        # 步骤4: 测试代理延迟并找到最佳代理（支持参数调优）
        max_test_count = int(os.getenv('MAX_PROXY_TEST_COUNT', '5'))
        min_success_rate = float(os.getenv('MIN_SUCCESS_RATE', '0.8'))
        max_latency_ms = int(os.getenv('MAX_LATENCY_MS', '5000'))
        test_count = int(os.getenv('TEST_COUNT', '3'))
        timeout = int(os.getenv('TEST_TIMEOUT', '10'))
        concurrency = int(os.getenv('TEST_CONCURRENCY', '5'))

        # 可选地允许自定义测试 URL 列表（以逗号分隔）
        custom_test_urls = os.getenv('TEST_URLS')
        test_urls = None
        if custom_test_urls:
            test_urls = [u.strip() for u in custom_test_urls.split(',') if u.strip()]
        
        logger.info("开始测试代理延迟，选择最佳代理...")
        best_proxy_info = find_best_proxy_by_latency_enhanced(
            proxies,
            max_test_count=max_test_count,
        )
        
        if not best_proxy_info:
            logger.error("❌ 未找到符合条件的代理，任务结束。")
            return
        
        ip_port = best_proxy_info['proxy']
        user = best_proxy_info['user']
        host, port = ip_port.split(':')
        password = "1"
        
        # 对用户名进行URL编码以处理特殊字符
        from urllib.parse import quote
        encoded_user = quote(user)
        proxy_url = f"http://{encoded_user}:{password}@{host}:{port}"
        
        logger.info(f"✅ 选择最佳代理，准备使用：{proxy_url}")
        logger.info(f"📊 代理性能统计：")
        logger.info(f"   - 平均延迟: {best_proxy_info['avg_latency_ms']:.1f}ms")
        logger.info(f"   - 成功率: {best_proxy_info['success_rate']:.1%}")
        logger.info(f"   - 测试次数: {best_proxy_info['working_tests']}/{best_proxy_info['test_count']}")
        
        # 步骤5: 更新渠道代理
        update_channel_proxy(
            base_url=required_env_vars['BASE_URL'],
            admin_id=required_env_vars['ADMIN_ID'],
            admin_token=required_env_vars['ADMIN_TOKEN'],
            channel_ids=channel_ids,
            proxy_url=proxy_url
        )
        
        # 保存新的MD5值到缓存
        save_cached_md5(current_md5)
        logger.info("✅ 代理更新任务完成！")
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON解析错误：{str(e)}")
    except Exception as e:
        logger.error(f"❌ 任务执行失败：{str(e)}")


def run_scheduled_task():
    """定时任务入口"""
    logger.info(f"Cron任务触发！开始执行：{datetime.now()}")
    main()


if __name__ == "__main__":
    main()
