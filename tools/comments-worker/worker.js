// 审片意见 API：GET 列出 / POST 提交 / DELETE 删除（需管理令牌）
// 存储：Cloudflare KV，key = comments:<slug>，value = JSON 数组
const ALLOWED_ORIGINS = [
  'https://video.guangai.ai',
  'http://localhost:8000',
  'http://127.0.0.1:8000',
];
const MAX_NAME = 24;
const MAX_TEXT = 2000;
const MAX_PER_SLUG = 500; // 每个项目最多保留的意见条数（最旧的被淘汰）

function json(data, status, corsHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin') || '';
    const cors = {
      'Access-Control-Allow-Origin': ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0],
      'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type,X-Admin-Token',
      'Access-Control-Max-Age': '86400',
    };
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

    // 路径即项目 slug：/<slug>，如 /zouzhipeng-packaging
    const slug = url.pathname.replace(/^\/+|\/+$/g, '');
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(slug)) return json({ error: 'bad slug' }, 400, cors);
    const key = 'comments:' + slug;

    if (request.method === 'GET') {
      const items = (await env.COMMENTS.get(key, 'json')) || [];
      return json({ comments: items }, 200, cors);
    }

    if (request.method === 'POST') {
      let body;
      try { body = await request.json(); } catch { return json({ error: 'bad json' }, 400, cors); }
      const t = String((body && body.t) || '').trim();
      if (!t || t.length > MAX_TEXT) return json({ error: 'invalid text' }, 400, cors);
      const item = {
        id: crypto.randomUUID(),
        n: String((body && body.n) || '').trim().slice(0, MAX_NAME),
        t,
        ts: Date.now(),
      };
      if (typeof body.tc === 'number' && isFinite(body.tc) && body.tc >= 0) {
        item.tc = Math.round(body.tc * 10) / 10;
      }
      const items = (await env.COMMENTS.get(key, 'json')) || [];
      items.push(item);
      if (items.length > MAX_PER_SLUG) items.splice(0, items.length - MAX_PER_SLUG);
      await env.COMMENTS.put(key, JSON.stringify(items));
      return json({ ok: true, comment: item }, 200, cors);
    }

    if (request.method === 'DELETE') {
      if (!env.ADMIN_TOKEN || request.headers.get('X-Admin-Token') !== env.ADMIN_TOKEN) {
        return json({ error: 'forbidden' }, 403, cors);
      }
      const id = url.searchParams.get('id') || '';
      const items = (await env.COMMENTS.get(key, 'json')) || [];
      await env.COMMENTS.put(key, JSON.stringify(items.filter((x) => x.id !== id)));
      return json({ ok: true }, 200, cors);
    }

    return json({ error: 'method not allowed' }, 405, cors);
  },
};
