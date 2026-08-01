#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传网页预览视频/成片到阿里云 OSS（guanghe-ai，成都）—— 国内骨干直连，稳定快速。

历史背景：R2 S3 端点在本机网络下 TLS 100% 被重置、境外上行存在时变 QoS（突发即断），
故 ingest 点迁入国内 OSS。本脚本直接对 OSS REST API 做 V1 签名分片上传，无任何依赖。

特性：并行分片、断点续传（状态绑定文件+key+分片大小指纹）、指数退避重试、
可选发送限速（令牌桶，--rate；受阻自动降速 AIMD）、uploadId 失效自动重建、
完成后按 HeadObject 字节数校验。预览对象自动设对象级 ACL 公共读，上传完即可引用。
可选让 VPS 从 OSS 公共地址拉取副本（服务器间链路，curl -C - 续传）。

用法:
    python tools/upload_video.py <本地视频> <slug> [选项]
    python tools/upload_video.py --delete <key>            # 删除 OSS 对象
例:
    python tools/upload_video.py p/zouzhipeng-packaging/assets/film_web.mp4 zouzhipeng-packaging
    python tools/upload_video.py output/成片.mp4 zouzhipeng-packaging --key zouzhipeng-packaging/final.mp4 --target all

选项:
    --key      OSS 对象 key，默认 <slug>/film_web.mp4
    --target   oss（默认）| vps | all；vps/all 在 OSS 完成后让 VPS 拉取副本到 /var/www/media
    --chunk    分片大小（如 8M），默认 8M
    --jobs     并发分片数，默认 4
    --rate     发送限速 KB/s（共享），默认 0 = 不限速（国内 OSS 一般不需要）；
               设置后若传输受阻自动降速（最低 30KB/s），稳定后缓慢回升
    --vps-dir  VPS 存放目录，默认 /var/www/media

凭据:
    ~/.deploy/oss.env         OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET / OSS_ENDPOINT
    ~/.deploy/depoysvr.env    DEPOYSVR_HOST / DEPOYSVR_USER / DEPOYSVR_PORT（--target vps|all 时）
    ~/.deploy/depoysvr_ed25519  VPS 私钥
完成后引用: https://<bucket>.<endpoint>/<key>（对象级公共读，同名覆盖即生效）
"""
import argparse
import base64
import email.utils
import hashlib
import hmac
import http.client
import json
import os
import posixpath
import queue
import random
import re
import shlex
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from xml.etree import ElementTree

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,127}\.mp4$")


class Fatal(Exception):
    pass


class StaleUpload(Exception):
    pass


def load_kv(path):
    env = {}
    p = os.path.expanduser(path)
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get_oss_cfg():
    cfg = load_kv("~/.deploy/oss.env")
    missing = [k for k in ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT")
               if not cfg.get(k)]
    if missing:
        raise Fatal(f"~/.deploy/oss.env 缺少: {', '.join(missing)}")
    return cfg


def parse_size(s):
    m = re.fullmatch(r"(\d+)\s*([KkMmGg]?)", s.strip())
    if not m:
        raise Fatal(f"无法解析分片大小: {s}")
    return int(m.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]


def fmt_mb(n):
    return f"{n / 1048576:.1f}"


def fmt_eta(sec):
    if sec == float("inf") or sec != sec:
        return "--:--"
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _xml_text(body, tag):
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    for el in root.iter():
        if el.tag.endswith(tag):
            return el.text
    return None


class Throttle:
    """跨线程共享令牌桶限速器（bytes/s），支持运行中调速（AIMD 自适应）。"""

    def __init__(self, rate):
        self.rate = float(rate)
        self.allowance = float(rate)
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def set_rate(self, rate):
        with self.lock:
            self.rate = float(rate)
            self.allowance = min(self.allowance, self.rate)
            self.t = time.monotonic()

    def wait(self, n):
        while True:
            with self.lock:
                now = time.monotonic()
                self.allowance = min(self.rate, self.allowance + (now - self.t) * self.rate)
                self.t = now
                if self.allowance >= n:
                    self.allowance -= n
                    return
                need = (n - self.allowance) / self.rate
            time.sleep(min(need, 0.5))


class OssClient:
    """OSS REST API V1 签名客户端（虚拟主机风格，无第三方依赖）。"""

    def __init__(self, cfg):
        self.ak = cfg["OSS_ACCESS_KEY_ID"]
        self.sk = cfg["OSS_ACCESS_KEY_SECRET"]
        self.bucket = cfg["OSS_BUCKET"]
        self.endpoint = cfg["OSS_ENDPOINT"]
        self.host = f"{self.bucket}.{self.endpoint}"
        self.public_base = f"https://{self.host}"
        self._local = threading.local()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = http.client.HTTPSConnection(self.host, timeout=120)
            self._local.conn = c
        return c

    def drop_conn(self):
        self._local.conn = None

    def _sign(self, verb, qkey, sub, date, ctype="", oss_headers=()):
        ch = "".join(f"{k.lower()}:{v}\n" for k, v in sorted(oss_headers))
        resource = f"/{self.bucket}/{qkey}" + (f"?{sub}" if sub else "")
        sts = f"{verb}\n\n{ctype}\n{date}\n{ch}{resource}"
        return base64.b64encode(hmac.new(self.sk.encode(), sts.encode(), hashlib.sha1).digest()).decode()

    def request(self, verb, key, sub="", body=None, ctype="", oss_headers=(), paced=None):
        """单次请求。paced=Throttle 时手动分片限速发送 body。返回 (status, headers, body)。
        抛出的异常交由调用方分类。"""
        qkey = quote(key, safe="/")
        path = f"/{qkey}" + (f"?{sub}" if sub else "")
        date = email.utils.formatdate(usegmt=True)
        auth = f"OSS {self.ak}:{self._sign(verb, qkey, sub, date, ctype, oss_headers)}"
        c = self._conn()
        if paced is None:
            headers = {"Date": date, "Authorization": auth, "Host": self.host}
            for k, v in oss_headers:
                headers[k] = v
            if ctype:
                headers["Content-Type"] = ctype
            c.request(verb, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, r.headers, r.read()
        c.putrequest(verb, path)
        c.putheader("Date", date)
        c.putheader("Authorization", auth)
        for k, v in oss_headers:
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(body)))
        c.endheaders()
        mv = memoryview(body)
        for off in range(0, len(mv), 65536):
            seg = mv[off:off + 65536]
            paced.wait(len(seg))
            c.send(seg)
        r = c.getresponse()
        return r.status, r.headers, r.read()

    # ---- 高层操作 ----
    def init_multipart(self, key):
        status, _, body = self.request("POST", key, "uploads", ctype="video/mp4")
        if status != 200:
            raise Fatal(f"创建分片任务失败 HTTP {status}: {body[:300]!r}")
        return _xml_text(body, "UploadId")

    def put_object_acl(self, key, acl="public-read"):
        status, _, body = self.request("PUT", key, "acl", oss_headers=(("x-oss-object-acl", acl),))
        if status != 200:
            raise Fatal(f"设置对象 ACL 失败 HTTP {status}: {body[:300]!r}")

    def complete_multipart(self, key, upload_id, parts):
        def quoted(e):
            return e if e.startswith('"') else f'"{e}"'
        xml = ("<CompleteMultipartUpload>" +
               "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>{quoted(e)}</ETag></Part>" for n, e in parts) +
               "</CompleteMultipartUpload>").encode()
        status, _, body = self.request("POST", key, f"uploadId={quote(upload_id, safe='')}",
                                       body=xml, ctype="application/xml")
        if status == 404:
            raise StaleUpload()
        if status != 200:
            raise Fatal(f"合并失败 HTTP {status}: {body[:300]!r}")

    def abort_multipart(self, key, upload_id):
        self.request("DELETE", key, f"uploadId={quote(upload_id, safe='')}")

    def head(self, key):
        status, headers, _ = self.request("HEAD", key)
        if status != 200:
            return None
        return int(headers.get("Content-Length") or 0)

    def delete(self, key):
        status, _, body = self.request("DELETE", key)
        if status not in (204, 404):
            raise Fatal(f"删除失败 HTTP {status}: {body[:300]!r}")


class Uploader:
    def __init__(self, src, slug, key, chunk, jobs, oss, rate_kbs=0):
        self.src, self.slug, self.key = src, slug, key
        self.chunk, self.jobs, self.oss = chunk, jobs, oss
        self.throttle = Throttle(rate_kbs * 1024) if rate_kbs > 0 else None
        self.rate_cap = float(rate_kbs * 1024)
        self.rate_floor = 30.0 * 1024
        self._streak = 0
        self.size = os.path.getsize(src)
        self.mtime = int(os.path.getmtime(src))
        self.nparts = max(1, (self.size + chunk - 1) // chunk)
        tag = hashlib.sha1(f"{key}|{os.path.basename(src)}".encode()).hexdigest()[:12]
        self.state_path = os.path.join(os.path.dirname(os.path.abspath(src)), f".upload_state_{tag}.json")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stale = threading.Event()
        self._report_stop = threading.Event()
        self.errors = []
        self.done_bytes = 0

    # ---- 断点状态（绑定文件+key+分片大小指纹，防跨文件混拼） ----
    def save_state(self, st):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, self.state_path)

    def fresh_state(self):
        upload_id = self.oss.init_multipart(self.key)
        st = {"file": os.path.basename(self.src), "size": self.size, "mtime": self.mtime,
              "key": self.key, "chunk": self.chunk, "upload_id": upload_id, "parts": {}}
        self.save_state(st)
        return st

    def load_state(self):
        if os.path.exists(self.state_path):
            try:
                st = json.load(open(self.state_path, encoding="utf-8"))
            except (ValueError, OSError):
                st = None
            if st and st.get("file") == os.path.basename(self.src) and st.get("size") == self.size \
                    and st.get("mtime") == self.mtime and st.get("key") == self.key \
                    and st.get("chunk") == self.chunk and st.get("upload_id"):
                print(f"续传：已完成 {len(st['parts'])}/{self.nparts} 分片", flush=True)
                return st
            print("检测到不匹配的断点状态（文件/key/分片大小已变），忽略并新建任务。", flush=True)
        return self.fresh_state()

    # ---- 分片上传 ----
    def put_part(self, n, data, upload_id):
        sub = f"partNumber={n}&uploadId={quote(upload_id, safe='')}"
        for i in range(6):
            if self._stop.is_set():
                raise Fatal("已停止")
            status, body = 0, b""
            try:
                if self.throttle is None:
                    status, headers, body = self.oss.request("PUT", self.key, sub, body=data)
                else:
                    status, headers, body = self.oss.request("PUT", self.key, sub, body=data,
                                                             paced=self.throttle)
            except Exception as e:
                self.oss.drop_conn()  # 连接作废，下次重建
                body = f"{type(e).__name__}: {e}".encode()
                self._degrade()  # 传输受阻：AIMD 降速
            if status == 200:
                return headers.get("ETag", "").strip('"')
            if status == 404:
                self._stale.set()
                self._stop.set()
                raise StaleUpload()
            if status in (400, 403):
                raise Fatal(f"分片 {n} HTTP {status}: {body[:300]!r}")
            if i == 5:
                raise Fatal(f"分片 {n} 重试 6 次仍失败: HTTP {status} {body[:200]!r}")
            wait = min(30, 2 ** i) + random.random()
            print(f"\n  分片 {n} 失败（HTTP {status} {body[:80]!r}），{wait:.0f}s 后重试", flush=True)
            time.sleep(wait)

    def _degrade(self):
        if self.throttle is None:
            return
        with self._lock:
            self._streak = 0
            cur = self.throttle.rate
            new = max(self.rate_floor, cur * 0.6)
            if new < cur:
                self.throttle.set_rate(new)
                print(f"\n  传输受阻，限速降至 {new / 1024:.0f}KB/s", flush=True)

    def _on_part_ok(self):
        if self.throttle is None:
            return
        with self._lock:
            self._streak += 1
            if self._streak >= 8:
                self._streak = 0
                cur = self.throttle.rate
                new = min(self.rate_cap, cur * 1.25)
                if new > cur:
                    self.throttle.set_rate(new)
                    print(f"\n  传输稳定，限速升至 {new / 1024:.0f}KB/s", flush=True)

    def worker(self, q, st):
        while not self._stop.is_set():
            try:
                n = q.get_nowait()
            except queue.Empty:
                return
            try:
                with open(self.src, "rb") as f:
                    f.seek((n - 1) * self.chunk)
                    data = f.read(self.chunk)
                etag = self.put_part(n, data, st["upload_id"])
                with self._lock:
                    st["parts"][str(n)] = etag
                    self.save_state(st)
                    self.done_bytes += len(data)
                self._on_part_ok()
            except StaleUpload:
                return
            except Exception as e:
                with self._lock:
                    self.errors.append(f"分片 {n}: {e}")
                self._stop.set()
                return

    def report(self):
        last, t_last = self.done_bytes, time.time()
        while not self._report_stop.wait(2.5):
            with self._lock:
                done = self.done_bytes
            now = time.time()
            rate = (done - last) / max(now - t_last, 0.1)
            last, t_last = done, now
            pct = done / self.size * 100
            eta = (self.size - done) / rate if rate > 0 else float("inf")
            print(f"\r{fmt_mb(done)}/{fmt_mb(self.size)}MB（{pct:.0f}%）| {rate / 1048576:.1f}MB/s | ETA {fmt_eta(eta)}  ",
                  end="", flush=True)

    # ---- 主流程 ----
    def run(self):
        st = self.load_state()
        for _ in range(3):
            missing = [n for n in range(1, self.nparts + 1) if str(n) not in st["parts"]]
            if missing:
                print(f"{os.path.basename(self.src)} → {self.key}：{fmt_mb(self.size)}MB，"
                      f"{self.nparts} 分片，待传 {len(missing)}，{self.jobs} 并发", flush=True)
                self.done_bytes = min(len(st["parts"]) * self.chunk, self.size)
                self._stop.clear()
                self._stale.clear()
                self.errors = []
                self._report_stop.clear()
                q = queue.Queue()
                for n in missing:
                    q.put(n)
                rep = threading.Thread(target=self.report, daemon=True)
                rep.start()
                ts = [threading.Thread(target=self.worker, args=(q, st))
                      for _ in range(min(self.jobs, len(missing)))]
                try:
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join()
                except KeyboardInterrupt:
                    self._stop.set()
                    for t in ts:
                        t.join()
                    self._report_stop.set()
                    print(f"\n已中断。断点已保存（{len(st['parts'])}/{self.nparts} 分片），重跑同一命令即可续传。", flush=True)
                    sys.exit(2)
                self._report_stop.set()
                print(flush=True)
                if self.errors:
                    raise Fatal("；".join(self.errors))
                if self._stale.is_set():
                    print("uploadId 已失效，自动重建任务并续传…", flush=True)
                    st = self.fresh_state()
                    continue
            try:
                self.complete(st)
            except StaleUpload:
                print("合并时 uploadId 已失效，自动重建任务并重传…", flush=True)
                st = self.fresh_state()
                continue
            return
        raise Fatal("uploadId 连续失效，放弃")

    def complete(self, st):
        parts = [(int(n), e) for n, e in sorted(st["parts"].items(), key=lambda x: int(x[0]))]
        self.oss.complete_multipart(self.key, st["upload_id"], parts)
        self.oss.put_object_acl(self.key, "public-read")  # 预览对象需匿名可读（Init 不支持设 ACL）
        remote = self.oss.head(self.key)
        if remote is not None and remote != self.size:
            raise Fatal(f"校验失败：远端 {remote} 字节 ≠ 本地 {self.size} 字节")
        try:
            os.remove(self.state_path)
        except OSError:
            pass
        url = f"{self.oss.public_base}/{quote(self.key, safe='/')}"
        print(f"完成，校验通过。引用地址: {url}", flush=True)


def vps_pull(key, vps_dir, oss):
    env = load_kv("~/.deploy/depoysvr.env")
    host = env.get("VPS_HOST") or env.get("DEPOYSVR_HOST")
    user = env.get("VPS_USER") or env.get("DEPOYSVR_USER")
    port = env.get("VPS_PORT") or env.get("DEPOYSVR_PORT")
    if not host or not user:
        raise Fatal("~/.deploy/depoysvr.env 缺少 VPS_HOST/VPS_USER")
    keyfile = os.path.expanduser("~/.deploy/depoysvr_ed25519")
    rpath = f"{vps_dir.rstrip('/')}/{key}"
    url = f"{oss.public_base}/{quote(key, safe='/')}"  # 对象公共读，直拉
    # 先拉到 .part 再原子改名：续传只对“同一对象的半截下载”有效，
    # 避免同名旧版本文件触发 curl (33) 字节范围错误
    remote_cmd = ("mkdir -p " + shlex.quote(posixpath.dirname(rpath)) +
                  " && curl -fSL -C - --retry 5 --retry-all-errors --connect-timeout 15 -o "
                  + shlex.quote(rpath + ".part") + " " + shlex.quote(url) +
                  " && mv -f " + shlex.quote(rpath + ".part") + " " + shlex.quote(rpath))
    print(f"VPS 拉取：{user}@{host} ← {url}", flush=True)
    ssh = ["ssh", "-i", keyfile, "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ServerAliveInterval=15"]
    if port:
        ssh += ["-p", port]
    r = subprocess.run(ssh + [f"{user}@{host}", remote_cmd])
    if r.returncode != 0:
        raise Fatal(f"VPS 拉取失败（ssh 退出码 {r.returncode}）")
    print(f"VPS 副本完成：{rpath}", flush=True)


def delete_key(key, oss):
    if not KEY_RE.match(key):
        raise Fatal(f"key 格式非法: {key}")
    oss.delete(key)
    print(f"已删除 OSS 对象: {key}", flush=True)


def main():
    p = argparse.ArgumentParser(description="上传视频到阿里云 OSS（分片断点续传），可选 VPS 拉取副本")
    p.add_argument("src", nargs="?", help="本地视频路径")
    p.add_argument("slug", nargs="?", help="项目 slug（默认 key 为 <slug>/film_web.mp4）")
    p.add_argument("--key", help="OSS 对象 key（.mp4 结尾）")
    p.add_argument("--target", choices=["oss", "vps", "all"], default="oss")
    p.add_argument("--chunk", default="8M")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--rate", type=int, default=0, help="发送限速 KB/s（共享），默认 0=不限速；受阻自动降速")
    p.add_argument("--vps-dir", default="/var/www/media")
    p.add_argument("--delete", metavar="KEY", help="删除 OSS 对象后退出")
    args = p.parse_args()
    try:
        oss = OssClient(get_oss_cfg())
        if args.delete:
            delete_key(args.delete, oss)
            return
        if not args.src or not args.slug:
            p.error("缺少 <本地视频> 或 <slug>")
        if not SLUG_RE.match(args.slug):
            raise Fatal(f"slug 格式非法: {args.slug}")
        key = args.key or f"{args.slug}/film_web.mp4"
        if not KEY_RE.match(key):
            raise Fatal(f"key 格式非法（需小写字母数字路径、.mp4 结尾）: {key}")
        if not os.path.isfile(args.src) and args.target != "vps":
            raise Fatal(f"文件不存在: {args.src}")
        chunk = parse_size(args.chunk)
        if chunk < 1024 ** 2:
            raise Fatal("分片不能小于 1M")
        if chunk > 64 * 1024 ** 2:
            raise Fatal("分片不建议超过 64M（弱网单请求越大越容易失败）")
        if args.target == "vps":
            vps_pull(key, args.vps_dir, oss)
            return
        Uploader(args.src, args.slug, key, chunk, max(1, args.jobs), oss,
                 rate_kbs=max(0, args.rate)).run()
        if args.target == "all":
            vps_pull(key, args.vps_dir, oss)
    except Fatal as e:
        print(f"失败：{e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
