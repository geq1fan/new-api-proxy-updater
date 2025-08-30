#!/usr/bin/env python3
"""
New-API Channel Proxy Updater
一个定时任务，用于定期更新指定渠道的代理IP

代理来源：https://github.com/TopChina/proxy-list/blob/main/README.md

代理测试说明：
- 代理每小时更新一次用户名，密码默认为1
- 使用Proxy-Authorization请求头测试代理连通性
- 自动遍历代理列表，获取第一个通过测试的代理

环境变量：
- BASE_URL: New-API实例的URL
- ADMIN_ID: 管理员用户ID
- ADMIN_TOKEN: 管理员访问令牌
- CHANNEL_IDS: 需要更新代理的渠道ID列表
- PROXY_REGION: 代理地区 (默认: 香港)
- MAX_PROXY_TEST_COUNT: 最大代理测试数量 (默认: 5)
"""

import os
import json
import re
import requests
import logging
import sys
import base64
from typing import List, Tuple, Optional
from datetime import datetime
import time

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


def test_proxy_connectivity(proxy_ip: str, proxy_port: str, proxy_user: str, proxy_password: str = "1", 
                          test_url: str = "http://ip.im/info", timeout: int = 10) -> bool:
    """
    测试代理的连通性
    
    Args:
        proxy_ip: 代理IP地址
        proxy_port: 代理端口
        proxy_user: 代理用户名
        proxy_password: 代理密码 (默认为1)
        test_url: 测试URL (默认为ip.im/info)
        timeout: 超时时间 (秒)
    
    Returns:
        bool: 代理是否可用
    """
    try:
        # 构建代理URL
        from urllib.parse import quote
        encoded_user = quote(proxy_user)
        proxy_url = f"http://{encoded_user}:{proxy_password}@{proxy_ip}:{proxy_port}"
        
        # 设置代理
        proxies = {
            'http': proxy_url,
            'https': proxy_url
        }
        
        # 构建认证头
        auth_string = f"{proxy_user}:{proxy_password}"
        auth_bytes = auth_string.encode('utf-8')
        auth_header = f"Basic {base64.b64encode(auth_bytes).decode('utf-8')}"
        
        headers = {
            'Proxy-Authorization': auth_header,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        logger.debug(f"正在测试代理: {proxy_ip}:{proxy_port}")
        
        # 发送测试请求
        response = requests.get(
            test_url,
            proxies=proxies,
            headers=headers,
            timeout=timeout,
            verify=False  # 跳过SSL验证以提高成功率
        )
        
        if response.status_code == 200:
            logger.debug(f"✅ 代理 {proxy_ip}:{proxy_port} 测试成功")
            return True
        else:
            logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 测试失败: HTTP {response.status_code}")
            return False
            
    except requests.exceptions.Timeout:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 测试超时")
        return False
    except requests.exceptions.ProxyError:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 代理错误")
        return False
    except requests.exceptions.ConnectionError:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 连接错误")
        return False
    except Exception as e:
        logger.debug(f"❌ 代理 {proxy_ip}:{proxy_port} 测试异常: {str(e)}")
        return False


def find_working_proxy(proxies: List[Tuple[str, str]], max_test_count: int = 5) -> Optional[Tuple[str, str]]:
    """
    遍历代理列表，找到第一个可用的代理
    
    Args:
        proxies: 代理列表，每个元素为(ip_port, user)元组
        max_test_count: 最大测试数量 (默认为5)
    
    Returns:
        Optional[Tuple[str, str]]: 第一个可用的代理，如果没有则返回None
    """
    logger.info(f"开始测试代理连通性，共 {len(proxies)} 个代理，最多测试 {max_test_count} 个")
    
    tested_count = 0
    for i, (ip_port, user) in enumerate(proxies):
        if tested_count >= max_test_count:
            logger.info(f"已达到最大测试数量 {max_test_count}，停止测试")
            break
            
        logger.info(f"正在测试第 {i+1}/{len(proxies)} 个代理: {ip_port}")
        
        # 解析IP和端口
        try:
            proxy_ip, proxy_port = ip_port.split(':')
        except ValueError:
            logger.warning(f"无效的代理格式: {ip_port}")
            continue
        
        # 测试代理连通性
        if test_proxy_connectivity(proxy_ip, proxy_port, user):
            logger.info(f"✅ 找到可用代理: {ip_port}")
            return (ip_port, user)
        
        tested_count += 1
        # 添加短暂延迟避免频繁请求
        time.sleep(0.5)
    
    logger.warning(f"❌ 在 {len(proxies)} 个代理中未找到可用的代理")
    return None


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
                logger.error(f"❌ 渠道 {channel_id} 代理更新失败：HTTP {response.status}: {error_text}")
                raise Exception(f"代理更新失败：HTTP {response.status}: {error_text}")
                
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
        
        # 步骤4: 测试代理连通性并找到第一个可用的代理
        max_test_count = int(os.getenv('MAX_PROXY_TEST_COUNT', '5'))
        working_proxy = find_working_proxy(proxies, max_test_count)
        
        if not working_proxy:
            logger.error("❌ 未找到可用的代理，任务结束。")
            return
        
        ip_port, user = working_proxy
        host, port = ip_port.split(':')
        password = "1"
        
        # 对用户名进行URL编码以处理特殊字符
        from urllib.parse import quote
        encoded_user = quote(user)
        proxy_url = f"http://{encoded_user}:{password}@{host}:{port}"
        
        logger.info(f"✅ 找到可用代理，准备使用：{proxy_url}")
        
        # 步骤5: 更新渠道代理
        update_channel_proxy(
            base_url=required_env_vars['BASE_URL'],
            admin_id=required_env_vars['ADMIN_ID'],
            admin_token=required_env_vars['ADMIN_TOKEN'],
            channel_ids=channel_ids,
            proxy_url=proxy_url
        )
        
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