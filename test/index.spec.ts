import { describe, it, expect } from 'vitest';
import { extractProxiesByRegion } from '../src/index';

describe('extractProxiesByRegion', () => {
  it('should extract Hong Kong proxies', async () => {
	const response = await fetch("https://raw.githubusercontent.com/TopChina/proxy-list/refs/heads/main/README.md");
	const text = await response.text();
	expect(extractProxiesByRegion(text, "香港").length).toBeGreaterThan(0);
  });
});
