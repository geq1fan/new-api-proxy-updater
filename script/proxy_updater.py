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
        best_proxy_info = find_best_proxy_by_latency(
            proxies,
            max_test_count=max_test_count,
            min_success_rate=min_success_rate,
            max_latency_ms=max_latency_ms,
            test_urls=test_urls,
            test_count=test_count,
            timeout=timeout,
            concurrency=concurrency,
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
