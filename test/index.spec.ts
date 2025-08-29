import { describe, it, expect } from 'vitest';
import { extractHongKongProxies } from '../src/index';

describe('extractHongKongProxies', () => {
  it('should extract Hong Kong proxies', async () => {
	const response = await fetch("https://raw.githubusercontent.com/TopChina/proxy-list/refs/heads/main/README.md");
	const text = await response.text();
	expect(extractHongKongProxies(text).length).toBeGreaterThan(0);
  });
});
