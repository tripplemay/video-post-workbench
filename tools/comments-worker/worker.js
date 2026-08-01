// 审片意见 API：GET 列出 / POST 提交 / DELETE 删除（管理令牌，或评论自带的删除令牌哈希匹配）
// 存储：Cloudflare KV，key = comments:<slug>，value = JSON 数组
// 匿名删除权：提交方生成随机删除令牌，仅存其 SHA-256 哈希（h 字段）；GET 不返回 h，
// 删除时校验 sha256(X-Delete-Token) === h，因此只有持有令牌的提交者本人（或管理员）可删。
//
// 管理上传：浏览器 → 阿里云 OSS 直传（guanghe-ai，成都）。Worker 只做控制面：
// 代签 Init/Complete/Abort/Delete 与分片预签名 URL，数据面（分片 PUT）浏览器直连 OSS，
// 不经过 Worker —— 上传速度取决于用户到 OSS 的国内链路，与 CF 边缘无关。
const ALLOWED_ORIGINS = [
  'https://video.guangai.ai',
  'http://localhost:8000',
  'http://127.0.0.1:8000',
];
const MAX_NAME = 24;
const MAX_TEXT = 2000;
const MAX_PER_SLUG = 500; // 每个项目最多保留的意见条数（最旧的被淘汰）
const HASH_RE = /^[0-9a-f]{64}$/;
const KEY_RE = /^[a-z0-9][a-z0-9/_-]{0,127}\.mp4$/;
const PRESIGN_TTL = 3600; // 分片预签名 URL 有效期（秒）

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

// ---- OSS V1 签名 ----
function quoteKey(key) {
  return key.split('/').map(encodeURIComponent).join('/');
}

async function hmacSha1B64(secret, text) {
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(sig)));
}

function ossHost(env) {
  return `${env.OSS_BUCKET}.${env.OSS_ENDPOINT}`;
}

// 头部鉴权调用 OSS（Init/Complete/Abort/Delete/ACL 等小请求）
// ossHeaders 里只有 x-oss- 前缀的头才进入签名（CanonicalizedOSSHeaders），全部头都会发送
async function ossCall(env, verb, key, sub, body, ctype, ossHeaders) {
  const qkey = quoteKey(key);
  const date = new Date().toUTCString();
  const ch = Object.keys(ossHeaders || {})
    .filter((k) => k.toLowerCase().startsWith('x-oss-'))
    .map((k) => k.toLowerCase()).sort()
    .map((k) => `${k}:${ossHeaders[k]}\n`).join('');
  const resource = `/${env.OSS_BUCKET}/${qkey}` + (sub ? `?${sub}` : '');
  const sts = `${verb}\n\n${ctype || ''}\n${date}\n${ch}${resource}`;
  const sig = await hmacSha1B64(env.OSS_ACCESS_KEY_SECRET, sts);
  const headers = {
    'Date': date,
    'Authorization': `OSS ${env.OSS_ACCESS_KEY_ID}:${sig}`,
    ...(ossHeaders || {}),
  };
  if (ctype) headers['Content-Type'] = ctype;
  return fetch(`https://${ossHost(env)}/${qkey}` + (sub ? `?${sub}` : ''), {
    method: verb, headers, body: body || undefined,
  });
}

// 分片预签名 URL（查询串鉴权，浏览器直连 PUT）
async function ossPresignPart(env, key, uploadId, n, expires) {
  const qkey = quoteKey(key);
  const sub = `partNumber=${n}&uploadId=${uploadId}`;
  const sts = `PUT\n\n\n${expires}\n/${env.OSS_BUCKET}/${qkey}?${sub}`;
  const sig = await hmacSha1B64(env.OSS_ACCESS_KEY_SECRET, sts);
  return `https://${ossHost(env)}/${qkey}?${sub}&Expires=${expires}` +
    `&OSSAccessKeyId=${encodeURIComponent(env.OSS_ACCESS_KEY_ID)}` +
    `&Signature=${encodeURIComponent(sig)}`;
}

// 管理上传：OSS 分片上传控制面。op=start 创建 → op=sign 取分片预签名 → 浏览器直传 →
// op=complete 合并（含公共读 ACL 与 size 校验）→ op=abort 放弃 → op=delete 删除对象
async function handleUpload(request, env, url, slug, cors) {
  if (!env.ADMIN_TOKEN || request.headers.get('X-Admin-Token') !== env.ADMIN_TOKEN) {
    return json({ error: 'forbidden' }, 403, cors);
  }
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(slug)) return json({ error: 'bad slug' }, 400, cors);
  if (!env.OSS_ACCESS_KEY_ID || !env.OSS_BUCKET) return json({ error: 'oss not configured' }, 500, cors);
  const key = url.searchParams.get('key') || slug + '/film_web.mp4';
  if (!KEY_RE.test(key)) return json({ error: 'bad key' }, 400, cors);
  const op = url.searchParams.get('op') || '';
  const uploadId = url.searchParams.get('uploadId') || '';
  const publicUrl = `https://${ossHost(env)}/${quoteKey(key)}`;

  if (op === 'start' && request.method === 'POST') {
    const r = await ossCall(env, 'POST', key, 'uploads', null, 'video/mp4');
    const text = await r.text();
    if (!r.ok) return json({ error: 'oss-error', detail: text.slice(0, 300) }, 502, cors);
    const m = text.match(/<UploadId>([^<]+)<\/UploadId>/);
    if (!m) return json({ error: 'oss-error', detail: 'no UploadId' }, 502, cors);
    return json({ uploadId: m[1] }, 200, cors);
  }

  if (op === 'sign') {
    const from = parseInt(url.searchParams.get('from') || '0', 10);
    const count = Math.min(parseInt(url.searchParams.get('count') || '20', 10), 100);
    if (!uploadId || !(from >= 1)) return json({ error: 'bad sign' }, 400, cors);
    const expires = Math.floor(Date.now() / 1000) + PRESIGN_TTL;
    const urls = {};
    for (let n = from; n < from + count; n++) {
      urls[n] = await ossPresignPart(env, key, uploadId, n, expires);
    }
    return json({ urls, expires }, 200, cors);
  }

  if (op === 'complete' && request.method === 'POST') {
    let body;
    try { body = await request.json(); } catch { return json({ error: 'bad json' }, 400, cors); }
    if (!body.uploadId || !Array.isArray(body.parts)) return json({ error: 'bad parts' }, 400, cors);
    const parts = body.parts.map((p) => ({
      n: p.partNumber || p.PartNumber,
      e: String(p.etag || p.ETag || ''),
    }));
    const xml = '<CompleteMultipartUpload>' + parts.map((p) =>
      `<Part><PartNumber>${p.n}</PartNumber><ETag>${p.e.startsWith('"') ? p.e : `"${p.e}"`}</ETag></Part>`
    ).join('') + '</CompleteMultipartUpload>';
    const r = await ossCall(env, 'POST', key, `uploadId=${encodeURIComponent(body.uploadId)}`,
      xml, 'application/xml');
    const text = await r.text();
    if (r.status === 404) return json({ error: 'upload-not-found', detail: text.slice(0, 300) }, 409, cors);
    if (!r.ok) return json({ error: 'oss-error', detail: text.slice(0, 300) }, 502, cors);
    // 预览对象需匿名可读（Init 不支持设 ACL，Complete 后单独设置）
    await ossCall(env, 'PUT', key, 'acl', null, '', { 'x-oss-object-acl': 'public-read' });
    // HEAD 在 Workers 里拿不到 Content-Length，改用 Range GET 从 Content-Range 解析总大小
    const probe = await ossCall(env, 'GET', key, '', null, '', { 'Range': 'bytes=0-0' });
    const cr = probe.headers.get('Content-Range') || '';
    const cm = cr.match(/\/(\d+)\s*$/);
    const size = probe.ok && cm ? parseInt(cm[1], 10) : null;
    return json({ ok: true, key, url: publicUrl, size }, 200, cors);
  }

  if (op === 'abort' && request.method === 'POST') {
    let body = {};
    try { body = await request.json(); } catch { /* 忽略 */ }
    await ossCall(env, 'DELETE', key,
      `uploadId=${encodeURIComponent(String(body.uploadId || uploadId))}`);
    return json({ ok: true }, 200, cors);
  }

  if (op === 'delete' && request.method === 'POST') {
    const r = await ossCall(env, 'DELETE', key, '');
    if (!r.ok && r.status !== 404) {
      return json({ error: 'oss-error', detail: (await r.text()).slice(0, 300) }, 502, cors);
    }
    return json({ ok: true, key }, 200, cors);
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

    // 管理上传：/upload/<slug> —— OSS 直传控制面，全部需 X-Admin-Token
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
