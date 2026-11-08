import { afterEach, describe, expect, it, vi } from 'vitest';
import { readRunEventPage } from './eventLog';
afterEach(() => vi.unstubAllGlobals());
describe('事件读取错误分类', () => {
  it('保留服务端状态与错误码', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: '暂不可用', code: 'TEST_BUSY' } }), { status: 503 })));
    await expect(readRunEventPage('r', 0)).rejects.toThrow('503 · TEST_BUSY');
  });
  it('代理HTML响应不能当作没有记录', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>proxy error</html>', { status: 502 })));
    await expect(readRunEventPage('r', 0)).rejects.toThrow('响应无法解析（HTTP 502）');
  });
  it('拒绝其他运行事件和错误分页形状', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [{ run_id: 'other', seq: 1 }], next_cursor: null }))));
    await expect(readRunEventPage('r', 0)).rejects.toThrow('不符合执行记录契约');
  });
  it('网络失败与服务端失败分开显示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(readRunEventPage('r', 0)).rejects.toThrow('无法连接执行记录服务');
  });
});
