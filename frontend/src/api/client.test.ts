import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadHealth } from './client';

const ready = { status: 'ok', stage: 1, database: 'ready', fts5: 'available', worker: 'not_implemented', llm: 'not_configured', embedding: 'not_configured', analysis: 'not_implemented' };
afterEach(() => vi.unstubAllGlobals());
describe('health boundaries', () => {
  it('healthy without keys', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(ready))));
    expect((await loadHealth()).kind).toBe('ready');
  });
  it('migration diagnostic', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...ready, status: 'degraded', database: 'migration_required' }), { status: 503 })));
    expect((await loadHealth()).kind).toBe('degraded');
  });
  it('offline failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    expect((await loadHealth()).kind).toBe('error');
  });
  it('invalid proxy response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502 })));
    expect((await loadHealth()).kind).toBe('error');
  });
});
