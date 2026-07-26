#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传网页预览视频到 Cloudflare R2（guangai-media bucket）。视频不入 git 仓库。

用法:
    python tools/upload_video.py <本地视频路径> <对象key>
例:
    python tools/upload_video.py p/zouzhipeng-packaging/assets/film_web.mp4 zouzhipeng-packaging/film_web.mp4

凭据: 读取本机 ~/.deploy/r2.env（R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT）。
上传完成后用 CDN 域名引用:
    https://cdn.guangai.ai/<对象key>
依赖: boto3（pip install boto3，建议虚拟环境）。
"""
import json
import os
import sys
import time

import boto3
from botocore.config import Config

CHUNK = 16 * 1024 * 1024  # 16MB 分片


def load_env():
    env = {}
    for line in open(os.path.expanduser("~/.deploy/r2.env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    src, key = sys.argv[1], sys.argv[2]
    env = load_env()
    s3 = boto3.client(
        "s3", endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name="auto",
        config=Config(request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))
    bucket = env.get("R2_BUCKET", "guangai-media")
    state_path = os.path.join(os.path.dirname(src) or ".", ".r2_upload_state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}

    size = os.path.getsize(src)
    nparts = (size + CHUNK - 1) // CHUNK
    if "upload_id" not in state:
        r = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="video/mp4")
        state = {"upload_id": r["UploadId"], "parts": {}}
        json.dump(state, open(state_path, "w"))

    def put_part(n, data, tries=8):
        for i in range(tries):
            try:
                r = s3.upload_part(Bucket=bucket, Key=key, UploadId=state["upload_id"],
                                   PartNumber=n, Body=data)
                return r["ETag"]
            except Exception as e:
                if i == tries - 1:
                    raise
                w = min(20, 2 * (i + 1))
                print(f"  part {n} retry ({type(e).__name__}) wait {w}s", flush=True)
                time.sleep(w)

    t0 = time.time()
    with open(src, "rb") as f:
        for n in range(1, nparts + 1):
            if str(n) in state["parts"]:
                continue
            etag = put_part(n, f.read(CHUNK))
            state["parts"][str(n)] = etag
            json.dump(state, open(state_path, "w"))
            print(f"part {n}/{nparts} ok | {time.time()-t0:.0f}s", flush=True)

    parts = [{"PartNumber": int(n), "ETag": e}
             for n, e in sorted(state["parts"].items(), key=lambda x: int(x[0]))]
    s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=state["upload_id"],
                                 MultipartUpload={"Parts": parts})
    os.remove(state_path)
    print(f"完成。引用地址: https://cdn.guangai.ai/{key}")


if __name__ == "__main__":
    main()
