// 审片意见 API：GET 列出 / POST 提交 / DELETE 删除（管理令牌，或评论自带的删除令牌哈希匹配）
// 存储：Cloudflare KV，key = comments:<slug>，value = JSON 数组
// 匿名删除权：提交方生成随机删除令牌，仅存其 SHA-256 哈希（h 字段）；GET 不返回 h，
// 删除时校验 sha256(X-Delete-Token) === h，因此只有持有令牌的提交者本人（或管理员）可删。
const ALLOWED_ORIGINS = [
  'https://video.guangai.ai',
  'http://localhost:8000',
  'http://127.0.0.1:8000',
];
const MAX_NAME = 24;
const MAX_TEXT = 2000;
const MAX_PER_SLUG = 500; // 每个项目最多保留的意见条数（最旧的被淘汰）
const HASH_RE = /^[0-9a-f]{64}$/;

function json(data, status, corsHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders },
  });
}

async function sha256hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// 返回给客户端的评论不含删除令牌哈希
function publicItem(x) {
  const { h, ...rest } = x;
  return rest;
}

// 管理上传：R2 分片上传，目标固定为 <slug>/film_web.mp4（网页预览视频）
// op=start 创建任务 → op=part&n=N（PUT 单个分片）→ op=complete 合并 → op=abort 放弃
async function handleUpload(request, env, url, slug, cors) {
  if (!env.ADMIN_TOKEN || request.headers.get('X-Admin-Token') !== env.ADMIN_TOKEN) {
    return json({ error: 'forbidden' }, 403, cors);
  }
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(slug)) return json({ error: 'bad slug' }, 400, cors);
  const key = slug + '/film_web.mp4';
  const op = url.searchParams.get('op') || '';
  const uploadId = url.searchParams.get('uploadId') || '';

  if (op === 'start' && request.method === 'POST') {
    const up = await env.MEDIA.createMultipartUpload(key, {
      httpMetadata: { contentType: 'video/mp4' },
    });
    return json({ uploadId: up.uploadId }, 200, cors);
  }

  if (op === 'part' && request.method === 'PUT') {
    const n = parseInt(url.searchParams.get('n') || '0', 10);
    if (!uploadId || !(n >= 1 && n <= 10000)) return json({ error: 'bad part' }, 400, cors);
    const up = env.MEDIA.resumeMultipartUpload(key, uploadId);
    const part = await up.uploadPart(n, request.body);
    return json({ etag: part.etag }, 200, cors);
  }

  if (op === 'complete' && request.method === 'POST') {
    let body;
    try { body = await request.json(); } catch { return json({ error: 'bad json' }, 400, cors); }
    if (!body.uploadId || !Array.isArray(body.parts)) return json({ error: 'bad parts' }, 400, cors);
    // 兼容 S3 风格（PartNumber/ETag）与 binding 风格（partNumber/etag）
    const parts = body.parts.map((p) => ({
      partNumber: p.partNumber || p.PartNumber,
      etag: p.etag || p.ETag,
    }));
    const up = env.MEDIA.resumeMultipartUpload(key, String(body.uploadId));
    await up.complete(parts);
    return json({ ok: true, key, url: 'https://cdn.guangai.ai/' + key }, 200, cors);
  }

  if (op === 'abort' && request.method === 'POST') {
    let body = {};
    try { body = await request.json(); } catch { /* 忽略 */ }
    const up = env.MEDIA.resumeMultipartUpload(key, String(body.uploadId || uploadId));
    await up.abort();
    return json({ ok: true }, 200, cors);
  }

  return json({ error: 'bad op' }, 400, cors);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin') || '';
    const cors = {
      'Access-Control-Allow-Origin': ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0],
      'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type,X-Admin-Token,X-Delete-Token',
      'Access-Control-Max-Age': '86400',
    };
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

    // 管理上传：/upload/<slug> —— R2 分片上传 <slug>/film_web.mp4，全部需 X-Admin-Token
    const segs = url.pathname.replace(/^\/+|\/+$/g, '').split('/');
    if (segs[0] === 'upload') return handleUpload(request, env, url, segs[1] || '', cors);

    // 路径即项目 slug：/<slug>，如 /zouzhipeng-packaging
    const slug = segs.join('/');
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(slug)) return json({ error: 'bad slug' }, 400, cors);
    const key = 'comments:' + slug;

    if (request.method === 'GET') {
      const items = (await env.COMMENTS.get(key, 'json')) || [];
      return json({ comments: items.map(publicItem) }, 200, cors);
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
      // 删除令牌哈希（可选）：提交方持有令牌本体，此处只存哈希
      if (typeof body.h === 'string' && HASH_RE.test(body.h)) item.h = body.h;
      const items = (await env.COMMENTS.get(key, 'json')) || [];
      items.push(item);
      if (items.length > MAX_PER_SLUG) items.splice(0, items.length - MAX_PER_SLUG);
      await env.COMMENTS.put(key, JSON.stringify(items));
      return json({ ok: true, comment: publicItem(item) }, 200, cors);
    }

    if (request.method === 'DELETE') {
      const id = url.searchParams.get('id') || '';
      const items = (await env.COMMENTS.get(key, 'json')) || [];
      const target = items.find((x) => x.id === id);
      if (!target) return json({ error: 'not found' }, 404, cors);
      const isAdmin = env.ADMIN_TOKEN && request.headers.get('X-Admin-Token') === env.ADMIN_TOKEN;
      if (!isAdmin) {
        const dt = request.headers.get('X-Delete-Token') || '';
        const ok = target.h && dt && (await sha256hex(dt)) === target.h;
        if (!ok) return json({ error: 'forbidden' }, 403, cors);
      }
      await env.COMMENTS.put(key, JSON.stringify(items.filter((x) => x.id !== id)));
      return json({ ok: true }, 200, cors);
    }

    return json({ error: 'method not allowed' }, 405, cors);
  },
};
