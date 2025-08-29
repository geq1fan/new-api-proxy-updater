/**
 * 更新指定渠道的代理设置。
 * @param env - 包含 API URL 和凭证的环境变量。
 * @param proxyUrl - 要设置的新代理 URL。
 */
async function updateChannelProxy(env: Env, proxyUrl: string): Promise<void> {
	let channelIds
  try {
    channelIds = JSON.parse(env.CHANNEL_IDS) as number[];
  } catch (error) {
    throw new Error(`无效的 CHANNEL_IDS: ${env.CHANNEL_IDS}`);
  }
  const apiUrl = `${env.BASE_URL.replace(/\/$/, '')}/api/channel/`;
  
  for (const id of channelIds) {
    const updateData = {
      id,
      setting: JSON.stringify({ proxy: proxyUrl }),
    };
    console.log(`正在更新渠道 ${id} 的代理...`);
    const response = await fetch(apiUrl, {
      method: 'PUT',
      headers: {
        'New-Api-User': env.ADMIN_ID,
        'Authorization': `Bearer ${env.ADMIN_TOKEN}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(updateData),
    });
  
    if (response.ok) {
      console.log("✅ 代理更新成功！");
    } else {
      const errorText = await response.text();
      console.error(`❌ 代理更新失败：HTTP ${response.status}: ${errorText}`);
      // 抛出错误以确保外层 catch 能够捕获到
      throw new Error(`代理更新失败：HTTP ${response.status}: ${errorText}`);
    }
  }
}

export function extractHongKongProxies(markdownText: string): [string, string][] {
  const proxies: [string, string][] = [];
  const lines = markdownText.split('\n');
  const ipPortRegex = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$/;

  for (const line of lines) {
    // 检查是否为表格数据行
    if (line.startsWith('|') && !line.startsWith('|---')) {
      // 分割列并去除首尾空格
      const columns = line.split('|').map(col => col.trim()).filter(col => col);

      if (columns.length >= 3) {
        const ipPort = columns[0];
        const country = columns[1];
        const user = columns[2];

        // 筛选香港地区且 IP:端口 格式正确的代理
        if (country === "香港" && ipPortRegex.test(ipPort)) {
          proxies.push([ ipPort, user ]);
        }
      }
    }
  }
  return proxies;
}

// Cloudflare Worker 的主入口点
export default {
  // `scheduled` 函数会在 Cron Trigger 触发时自动执行
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    console.log(`Cron Trigger 触发！开始执行任务：${event.cron}`);

    // 步骤 1: 检查所有必要的环境变量是否已设置
    if (!env.BASE_URL || !env.ADMIN_ID || !env.ADMIN_TOKEN || !env.CHANNEL_IDS) {
      console.error("错误：一个或多个环境变量未设置 (BASE_URL, ADMIN_ID, ADMIN_TOKEN, CHANNEL_IDS)。请在 Cloudflare dashboard 或使用 wrangler secret 设置它们。");
      return; // 提前退出
    }

    try {
      // 步骤 2: 从 GitHub 获取 Markdown 代理列表
      const response = await fetch("https://raw.githubusercontent.com/TopChina/proxy-list/refs/heads/main/README.md");
      if (!response.ok) {
        throw new Error(`获取代理列表失败，状态码：${response.status}`);
      }
      const markdownText = await response.text();
      console.log("成功获取代理列表 Markdown 文件。");

      // 步骤 3: 解析 Markdown 并提取香港代理
      const proxies = extractHongKongProxies(markdownText);
      if (proxies.length === 0) {
        console.log("在列表中未找到有效的香港代理。任务结束。");
        return;
      }
      console.log(`找到了 ${proxies.length} 个香港代理。`);

      // 步骤 4: 选择第一个代理并构建代理 URL
      const firstProxy = proxies[0];
      const [ ipPort, user ] = firstProxy;
      const [host, port] = ipPort.split(":");
      const password = "1";
      // 使用 encodeURIComponent 对用户名进行 URL 编码，以处理特殊字符
      const encodedUser = encodeURIComponent(user);
      const proxyUrl = `http://${encodedUser}:${password}@${host}:${port}`;;
      console.log(`准备使用代理：${proxyUrl}`);

      // 步骤 5: 调用 New API 更新渠道代理
      await updateChannelProxy(env, proxyUrl);

    } catch (error) {
      // 捕获并记录任何在执行过程中发生的错误
      console.error("任务执行失败：", error instanceof Error ? error.message : String(error));
    }
  },
};
