#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从公开页面重建小宇宙播客 RSS（Apple Podcasts 规范）。"""

import argparse
import concurrent.futures
import datetime as dt
import gzip
import glob
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from email.utils import format_datetime, parsedate_to_datetime

API_BASE = "https://api.xiaoyuzhoufm.com"
WEB_BASE = "https://www.xiaoyuzhoufm.com"
REFRESH_PATH = "/app_auth_tokens.refresh"

UA_WEB = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
UA_WEB_ALT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
)
UA_APP = "Xiaoyuzhou/2.99.1(android 28)"

PAGE_SIZE = 25
MAX_PAGES = 200
SHOWNOTES_CAP = 60000
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"

DEFAULT_DEVICE_ID = "81ADBFD6-6921-482B-9AB9-A29E7CC7BB55"

APPLE_CATEGORIES = {
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government",
    "Health & Fitness", "History", "Kids & Family", "Leisure", "Music",
    "News", "Religion & Spirituality", "Science", "Society & Culture",
    "Sports", "Technology", "True Crime", "TV & Film",
}


def log(msg=""):
    print(msg, flush=True)


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


class RateLimiter:
    def __init__(self, per_second):
        self.gap = 1.0 / per_second if per_second and per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def set_rate(self, per_second):
        self.gap = 1.0 / per_second if per_second and per_second > 0 else 0.0

    def wait(self):
        if not self.gap:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.gap
            delay = slot - now
        if delay > 0:
            time.sleep(delay)


LIMITER = RateLimiter(12)
_THROTTLED_HOSTS = ("xiaoyuzhoufm.com", "xyzcdn.net")


# ───────────────────────────── HTTP ─────────────────────────────

def _decode(raw, content_encoding=""):
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if "deflate" in (content_encoding or "").lower():
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return raw
    return raw


def device_id_from_token(token):
    norm = re.sub(r"[^a-f0-9]", "", (token or "").lower())
    if len(norm) < 32:
        return DEFAULT_DEVICE_ID
    norm = norm[:32]
    return (
        f"{norm[:8]}-{norm[8:12]}-4{norm[13:16]}-a{norm[17:20]}-{norm[20:32]}"
    ).upper()


def _app_headers(access_token=None, device_id=None):
    now = dt.datetime.now().astimezone()
    headers = {
        "os": "android",
        "os-version": "28",
        "manufacturer": "Xiaomi",
        "model": "MI 6",
        "applicationid": "app.podcast.cosmos",
        "app-version": "2.99.1",
        "app-buildno": "1362",
        "User-Agent": UA_APP,
        "timezone": "Asia/Shanghai",
        "local-time": now.strftime("%Y-%m-%dT%H:%M:%S.000+0800"),
        "content-type": "application/json;charset=utf-8",
        "Accept-Encoding": "gzip",
        "x-jike-device-id": device_id or DEFAULT_DEVICE_ID,
    }
    if access_token:
        headers["x-jike-access-token"] = access_token
    return headers


def request(url, method="GET", payload=None, headers=None, timeout=30, retries=3):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if any(h in host for h in _THROTTLED_HOSTS):
        LIMITER.wait()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = dict(headers or {})
    if data:
        hdrs.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers, _decode(
                    resp.read(), resp.headers.get("Content-Encoding", "")
                )
        except urllib.error.HTTPError as e:
            last = e
            # WAF 403：换一个移动端 UA 再试（小宇宙对桌面 UA 会拦）
            if e.code == 403 and hdrs.get("User-Agent") == UA_WEB:
                hdrs["User-Agent"] = UA_WEB_ALT
                continue
            if e.code in (403, 429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"请求失败 {url}: {last}")


def get_text(url, headers=None, timeout=30):
    if headers is None:
        headers = {"User-Agent": UA_WEB, "Accept-Language": "zh-CN,zh;q=0.9"}
    return request(url, headers=headers, timeout=timeout)[2].decode("utf-8", "ignore")


def get_json(url, headers=None, timeout=30):
    return json.loads(get_text(url, headers=headers, timeout=timeout))


# ───────────────────────────── 页面 ─────────────────────────────

def parse_pid(value):
    value = (value or "").strip()
    m = re.search(r"/podcast/([0-9a-fA-F]{20,})", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{20,}", value):
        return value
    raise ValueError(f"解析不出 pid：{value!r}")


class Site:

    def __init__(self):
        self._build_id = None

    @property
    def build_id(self):
        if not self._build_id:
            m = re.search(r'"buildId":"([^"]+)"', get_text(f"{WEB_BASE}/"))
            if not m:
                raise RuntimeError("拿不到 buildId（前端结构可能变了）")
            self._build_id = m.group(1)
        return self._build_id

    def _next(self, path):
        for _ in range(2):
            try:
                return get_json(
                    f"{WEB_BASE}/_next/data/{self.build_id}/{path}.json",
                    headers={"User-Agent": UA_WEB, "Accept": "application/json"},
                )
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    self._build_id = None  # 发版后 buildId 会变
                    continue
                raise
        raise RuntimeError(f"_next/data 失败：{path}")

    def podcast(self, pid):
        podcast = self._next(f"podcast/{pid}")["pageProps"]["podcast"]
        return podcast, podcast.get("episodes") or []

    def episode(self, eid):
        try:
            return self._next(f"episode/{eid}")["pageProps"]["episode"]
        except Exception:
            page = get_text(f"{WEB_BASE}/episode/{eid}")
            m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
            if not m:
                raise RuntimeError(f"单集页无数据：{eid}")
            return json.loads(m.group(1))["props"]["pageProps"]["episode"]


# ───────────────────────────── 首次回填 ─────────────────────────────

class Credential:
    """只在回填时用；refresh token 换 access token，轮换后的新 refresh 自动回存。"""

    def __init__(self, refresh_token=None, access_token=None, device_id=None, token_path=None):
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.token_path = token_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "token.txt"
        )
        # 只算一次：token 轮换后不要重新派生，否则同一轮请求的设备号会漂移
        self.device_id = (
            device_id
            or os.environ.get("XYZ_DEVICE_ID", "").strip()
            or device_id_from_token(refresh_token or access_token)
        )
        self._expires = 0.0

    @classmethod
    def load(cls, args):
        if args.no_token:
            return None
        rt = (args.refresh_token or "").strip() or os.environ.get("XYZ_REFRESH_TOKEN", "").strip()
        at = (args.access_token or "").strip() or os.environ.get("XYZ_ACCESS_TOKEN", "").strip()
        path = args.token_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "token.txt"
        )
        if os.path.exists(path):
            value = open(path, encoding="utf-8").read().strip()
            if args.token_is_access:
                at = at or value
            else:
                rt = rt or value
        return cls(rt, at, args.device_id, path) if (rt or at) else None

    def _persist(self, refresh_token):
        try:
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            tmp = self.token_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(refresh_token + "\n")
            os.replace(tmp, self.token_path)
            log(f"  refresh token 已轮换，新的存到 {self.token_path}")
        except OSError as e:
            warn(f"  ! 新的 refresh token 存盘失败（本次运行不受影响）：{e}")
        self._sync_secret(refresh_token)

    @staticmethod
    def _sync_secret(refresh_token):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not repo or not os.environ.get("GH_TOKEN"):
            return
        try:
            import subprocess
            subprocess.run(
                ["gh", "secret", "set", "XYZ_REFRESH_TOKEN", "--repo", repo,
                 "--body", refresh_token],
                check=True, capture_output=True, timeout=60,
            )
            log("  Secret XYZ_REFRESH_TOKEN 已同步，下次运行不用手动换")
        except Exception as e:
            warn(f"  ! Secret 同步失败，下次运行前需要手动更新 XYZ_REFRESH_TOKEN：{e}")

    def token(self):
        # 只有 access token（session）时没有续期能力，一直用到它自己过期
        if self.access_token and not self.refresh_token:
            return self.access_token
        if self.access_token and time.time() < self._expires:
            return self.access_token
        if self.access_token and not self._expires:
            self._expires = time.time() + 90 * 60
            return self.access_token
        if not self.refresh_token:
            raise RuntimeError("没有可用的 refresh token：换一条新的再跑")
        headers = _app_headers(device_id=self.device_id)
        headers["x-jike-refresh-token"] = self.refresh_token
        # 官方客户端这个接口用的是 form 编码，不是 JSON
        headers["content-type"] = "application/x-www-form-urlencoded; charset=utf-8"
        _, resp_headers, body = request(API_BASE + REFRESH_PATH, method="POST", headers=headers)
        access = resp_headers.get("x-jike-access-token")
        if not access:
            try:
                access = json.loads(body or b"{}").get("x-jike-access-token")
            except ValueError:
                access = None
        if not access:
            raise RuntimeError("续期失败：refresh token 可能已失效")
        # 关键：小宇宙续期会轮换 refresh token，旧的立即作废
        # 不把新的存下来，2 小时后再续期、或者下次运行都会 401
        new_refresh = resp_headers.get("x-jike-refresh-token") or ""
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            self._persist(new_refresh)
        self.access_token = access
        self._expires = time.time() + 90 * 60
        return access

    def list_episodes(self, pid):
        out, cursor, pages = [], None, 0
        while pages < MAX_PAGES:
            payload = {"pid": pid, "limit": str(PAGE_SIZE), "order": "desc"}
            if cursor:
                payload["loadMoreKey"] = cursor
            token = self.token()
            try:
                _, _, body = request(
                    API_BASE + "/v1/episode/list", method="POST", payload=payload,
                    headers=_app_headers(token, self.device_id),
                )
            except urllib.error.HTTPError as e:
                if e.code == 401 and pages == 0:
                    self.access_token = None
                    self._expires = 0.0
                    continue
                raise
            resp = json.loads(body or b"{}")
            batch = resp.get("data") or []
            if isinstance(batch, dict):
                batch = batch.get("data") or []
            out.extend(batch)
            pages += 1
            cursor = resp.get("loadMoreKey")
            if not batch or not cursor:
                break
        return out


# ───────────────────────────── 规范化 ─────────────────────────────

def normalize(raw, podcast=None):
    enc = raw.get("enclosure") or {}
    media = raw.get("media") or {}
    src = media.get("source") or {}
    pod = raw.get("podcast") or podcast or {}
    image = (raw.get("image") or {}).get("picUrl") or (pod.get("image") or {}).get("picUrl") or ""
    duration = raw.get("duration")
    return {
        "eid": raw.get("eid") or "",
        "title": (raw.get("title") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "shownotes": (raw.get("shownotes") or "").strip(),
        "duration": int(duration) if isinstance(duration, (int, float)) else 0,
        "pub_date": raw.get("pubDate") or "",
        "audio_url": src.get("url") or enc.get("url") or "",
        "image": image,
        "private": bool(raw.get("isPrivateMedia")),
        "pay_type": raw.get("payType") or "FREE",
    }


def playable(ep):
    if not ep.get("audio_url") or not ep.get("eid"):
        return False
    if ep.get("private"):
        return False
    return ep.get("pay_type") in ("FREE", "PAY_EPISODE_WITH_FREE")


def audio_size(url):
    """enclosure 的 length 必须是真实字节数，否则 Apple 会判为无效单集。"""
    try:
        _, headers, _ = request(
            url, headers={"User-Agent": UA_APP, "Range": "bytes=0-0"}, retries=2, timeout=20
        )
        total = (headers.get("Content-Range") or "").rsplit("/", 1)[-1]
        if total.isdigit():
            return int(total)
        cl = headers.get("Content-Length") or ""
        if cl.isdigit():
            return int(cl)
    except Exception:
        pass
    try:
        _, headers, _ = request(url, method="HEAD", headers={"User-Agent": UA_APP}, retries=1, timeout=20)
        cl = headers.get("Content-Length") or ""
        if cl.isdigit():
            return int(cl)
    except Exception:
        pass
    return 0


# ───────────────────────────── 上一版 RSS = 存档 ─────────────────────────────

def read_feed(path):
    if not os.path.exists(path):
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        warn(f"  ! 旧 RSS 损坏，按空存档处理：{e}")
        return []
    out = []
    for item in root.findall("./channel/item"):
        guid = (item.findtext("guid") or "").strip()
        if not guid:
            continue
        enc = item.find("enclosure")
        pub = item.findtext("pubDate") or ""
        try:
            iso = parsedate_to_datetime(pub).astimezone(dt.timezone.utc).isoformat()
        except (TypeError, ValueError):
            iso = ""
        dur = item.findtext(f"{{{ITUNES}}}duration") or ""
        secs = 0
        if dur:
            for part in [p for p in dur.split(":") if p.strip().isdigit()]:
                secs = secs * 60 + int(part)
        img = item.find(f"{{{ITUNES}}}image")
        out.append({
            "eid": guid,
            "title": (item.findtext("title") or "").strip(),
            "shownotes": (item.findtext("description") or "").strip(),
            "description": "",
            "duration": secs,
            "pub_date": iso,
            "audio_url": enc.get("url") if enc is not None else "",
            "size": int(enc.get("length") or 0) if enc is not None else 0,
            "image": img.get("href") if img is not None else "",
            "private": False,
            "pay_type": "FREE",
        })
    return out


# ───────────────────────────── RSS ─────────────────────────────

def rfc822(value):
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        d = dt.datetime.now(dt.timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return format_datetime(d.astimezone(dt.timezone.utc))


def hms(seconds):
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def cdata(text):
    return "<![CDATA[" + (text or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def esc(text):
    """Apple 只认 XML 内置实体：&amp; &lt; &gt; &quot; &apos;，不用命名实体。"""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def mime(url):
    p = urllib.parse.urlparse(url or "").path.lower()
    if p.endswith((".m4a", ".mp4")):
        return "audio/mp4"
    if p.endswith(".aac"):
        return "audio/aac"
    if p.endswith((".ogg", ".opus")):
        return "audio/ogg"
    if p.endswith(".wav"):
        return "audio/wav"
    return "audio/mpeg"


def ep_number(title):
    m = re.match(r"^\s*(?:第)?(\d{1,4})\s*[.、|｜:：\-—]\s*", title or "")
    return int(m.group(1)) if m else 0


def render(meta, episodes, feed_url=""):
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    L = ['<?xml version="1.0" encoding="UTF-8"?>']
    L.append(
        '<rss version="2.0" '
        f'xmlns:itunes="{ITUNES}" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">'
    )
    L.append("<channel>")
    L.append(f"  <title>{esc(meta['title'])}</title>")
    L.append(f"  <link>{esc(meta['link'])}</link>")
    L.append(f"  <description>{cdata(meta['description'])}</description>")
    L.append("  <language>zh-CN</language>")
    L.append(f"  <lastBuildDate>{now}</lastBuildDate>")
    L.append("  <generator>dayuzhou</generator>")
    if meta.get("copyright"):
        L.append(f"  <copyright>{esc(meta['copyright'])}</copyright>")
    if feed_url:
        L.append(f'  <atom:link href="{esc(feed_url)}" rel="self" type="application/rss+xml"/>')
    if meta.get("author"):
        L.append(f"  <itunes:author>{esc(meta['author'])}</itunes:author>")
    L.append("  <itunes:type>episodic</itunes:type>")
    L.append(f"  <itunes:explicit>{'true' if meta.get('explicit') else 'false'}</itunes:explicit>")
    if meta.get("image"):
        L.append(f'  <itunes:image href="{esc(meta["image"])}"/>')
        L.append("  <image>")
        L.append(f"    <url>{esc(meta['image'])}</url>")
        L.append(f"    <title>{esc(meta['title'])}</title>")
        L.append(f"    <link>{esc(meta['link'])}</link>")
        L.append("  </image>")
    L.append(f'  <itunes:category text="{esc(meta["category"])}"/>')
    if meta.get("owner_name") or meta.get("owner_email"):
        L.append("  <itunes:owner>")
        if meta.get("owner_name"):
            L.append(f"    <itunes:name>{esc(meta['owner_name'])}</itunes:name>")
        if meta.get("owner_email"):
            L.append(f"    <itunes:email>{esc(meta['owner_email'])}</itunes:email>")
        L.append("  </itunes:owner>")

    for ep in episodes:
        body = ep.get("shownotes") or ""
        if not body and ep.get("description"):
            body = "<p>" + esc(ep["description"]).replace("\n", "<br/>") + "</p>"
        L.append("  <item>")
        L.append(f"    <title>{esc(ep['title'])}</title>")
        L.append(f"    <link>{WEB_BASE}/episode/{esc(ep['eid'])}</link>")
        L.append(f'    <guid isPermaLink="false">{esc(ep["eid"])}</guid>')
        L.append(f"    <pubDate>{rfc822(ep.get('pub_date'))}</pubDate>")
        if body:
            L.append(f"    <description>{cdata(body)}</description>")
        if ep.get("audio_url"):
            L.append(
                f'    <enclosure url="{esc(ep["audio_url"])}" length="{int(ep.get("size") or 0)}" '
                f'type="{mime(ep["audio_url"])}"/>'
            )
        if ep.get("duration"):
            L.append(f"    <itunes:duration>{hms(ep['duration'])}</itunes:duration>")
        if ep.get("image"):
            L.append(f'    <itunes:image href="{esc(ep["image"])}"/>')
        n = ep_number(ep["title"])
        if n:
            L.append(f"    <itunes:episode>{n}</itunes:episode>")
        L.append(f"    <itunes:explicit>{'true' if meta.get('explicit') else 'false'}</itunes:explicit>")
        L.append("  </item>")
    L.append("</channel>")
    L.append("</rss>")
    return "\n".join(L) + "\n"


def check_apple(meta, episodes):
    errors, warnings = [], []
    if not meta.get("title"):
        errors.append("channel 缺 title")
    elif len(meta["title"]) > 255:
        warnings.append("channel title 超过 255 字，部分客户端会截断")
    if not meta.get("link"):
        errors.append("channel 缺 link")
    if not meta.get("description"):
        errors.append("channel 缺 description")
    if not meta.get("image"):
        errors.append("channel 缺 itunes:image")
    if not str(meta.get("image", "")).startswith("https://"):
        errors.append("itunes:image 必须是 https 绝对地址")
    if meta.get("category") not in APPLE_CATEGORIES:
        errors.append(f"itunes:category 不在 Apple 分类白名单：{meta.get('category')!r}")
    if not meta.get("owner_email"):
        warnings.append("缺 itunes:owner/email（Apple 提交节目时需要，非必须才能播放）")
    if not episodes:
        errors.append("没有任何单集")
    no_size = [e["title"] for e in episodes if not e.get("size")]
    if no_size:
        errors.append(f"{len(no_size)} 集 enclosure 缺 length（Apple 会判无效）")
    bad_mime = [e["title"] for e in episodes if mime(e.get("audio_url")) == "audio/mpeg"
                and not (e.get("audio_url") or "").lower().endswith(".mp3")]
    if bad_mime:
        warnings.append(f"{len(bad_mime)} 集音频扩展名非常见格式，已按 audio/mpeg 标注")
    no_dur = sum(1 for e in episodes if not e.get("duration"))
    if no_dur:
        warnings.append(f"{no_dur} 集缺 itunes:duration")
    if len(episodes) < 1:
        errors.append("单集数为 0")
    return errors, warnings


# ───────────────────────────── 落盘 ─────────────────────────────

def write_text(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


_BUILD_STAMP = re.compile(r"<lastBuildDate>[^<]*</lastBuildDate>\s*")


def unchanged(path, new_text, volatile=(), normalize=None):
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        return False
    if normalize:
        old, new_text = normalize(old), normalize(new_text)
    else:
        old, new_text = _BUILD_STAMP.sub("", old), _BUILD_STAMP.sub("", new_text)
    for word in volatile:
        old = old.replace(word, "")
        new_text = new_text.replace(word, "")
    return old == new_text


def slug_of(cfg, pid):
    raw = (cfg.get("slug") or "").strip()
    if raw:
        return re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-") or pid[:12]
    return pid[:12]


# ───────────────────────────── 单个播客 ─────────────────────────────

def build(cfg, site, cred, args, results):
    pid = parse_pid(cfg["pid"])
    slug = slug_of(cfg, pid)
    feed_rel = f"feeds/{slug}.xml"
    feed_path = os.path.join(args.out, "feeds", f"{slug}.xml")
    label = cfg.get("title") or pid
    log(f"\n▶ {label}  ({slug})")

    try:
        podcast, latest = site.podcast(pid)
    except Exception as e:
        warn(f"  ✗ 页面抓取失败，保留上一版 RSS：{e}")
        results.append({"slug": slug, "pid": pid, "title": label, "error": str(e)})
        return

    archive = read_feed(feed_path)
    known = {ep["eid"]: ep for ep in archive}
    log(f"  页面 {len(latest)} 集｜存档 {len(known)} 集")

    fresh = [normalize(raw, podcast) for raw in latest if raw.get("eid")]
    fresh = [ep for ep in fresh if ep["title"]]

    need_backfill = args.backfill == "always" or (args.backfill == "auto" and not archive)
    backfill_error = ""
    if need_backfill:
        if not cred:
            log("  零账号模式：不回填历史，本次从最近 15 集起收录")
        else:
            try:
                raw_all = cred.list_episodes(pid)
                have = {ep["eid"] for ep in fresh} | set(known)
                added = 0
                for raw in raw_all:
                    ep = normalize(raw, podcast)
                    if ep["eid"] and ep["title"] and ep["eid"] not in have:
                        fresh.append(ep)
                        have.add(ep["eid"])
                        added += 1
                log(f"  回填：列表接口 {len(raw_all)} 集，补进 {added} 集")
            except Exception as e:
                backfill_error = str(e)
                warn(f"  ! 回填失败，继续用页面增量：{e}")

    new_ids = {ep["eid"] for ep in fresh if ep["eid"] not in known}
    log(f"  新增 {len(new_ids)} 集")

    targets = [ep for ep in fresh if ep["eid"] in new_ids]

    def enrich(ep):
        try:
            detail = normalize(site.episode(ep["eid"]), podcast)
        except Exception as e:
            warn(f"  ! 详情失败 {ep['eid']}: {e}")
            return ep
        merged = dict(ep)
        for key, value in detail.items():
            if value:
                merged[key] = value
        return merged

    if targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            targets = list(pool.map(enrich, targets))
    detail_map = {ep["eid"]: ep for ep in targets}

    merged = dict(known)
    for ep in fresh:
        ep = detail_map.get(ep["eid"], ep)
        prev = known.get(ep["eid"], {})
        ep["shownotes"] = (ep.get("shownotes") or prev.get("shownotes") or "")[:SHOWNOTES_CAP]
        ep["size"] = ep.get("size") or prev.get("size") or 0
        merged[ep["eid"]] = ep

    episodes = [ep for ep in merged.values() if playable(ep)]
    episodes.sort(key=lambda e: e.get("pub_date") or "", reverse=True)
    if cfg.get("max_episodes"):
        episodes = episodes[: int(cfg["max_episodes"])]
    if args.limit:
        episodes = episodes[: args.limit]

    missing = [ep for ep in episodes if not ep.get("size")]
    if missing:
        log(f"  补音频字节数 {len(missing)} 个…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            sizes = list(pool.map(lambda e: audio_size(e["audio_url"]), missing))
        for ep, size in zip(missing, sizes):
            if size:
                ep["size"] = size

    meta = {
        "title": cfg.get("title") or podcast.get("title") or label,
        "author": cfg.get("author") or podcast.get("author") or "",
        "description": cfg.get("description") or podcast.get("description")
        or podcast.get("brief") or "",
        "image": cfg.get("image") or (podcast.get("image") or {}).get("picUrl") or "",
        "link": f"{WEB_BASE}/podcast/{pid}",
        "category": cfg.get("category") or "Society & Culture",
        "explicit": bool(cfg.get("explicit")),
        "owner_name": cfg.get("owner_name") or "",
        "owner_email": cfg.get("owner_email") or "",
        "copyright": cfg.get("copyright") or "",
    }
    if meta["category"] not in APPLE_CATEGORIES:
        warn(f"  ! 分类 {meta['category']!r} 不在 Apple 白名单，已回退 Society & Culture")
        meta["category"] = "Society & Culture"

    feed_url = f"{args.base_url.rstrip('/')}/{feed_rel}" if args.base_url else ""
    xml_text = render(meta, episodes, feed_url)

    errors, warnings = check_apple(meta, episodes)
    for w in warnings:
        warn(f"  · {w}")
    for e in errors:
        warn(f"  ✗ Apple 规范：{e}")

    if args.dry_run:
        log(f"  ✓ {len(episodes)} 集（dry-run）")
    elif errors and args.strict:
        warn("  ✗ 存在硬性错误，未写入")
        results.append({"slug": slug, "pid": pid, "title": meta["title"], "error": "; ".join(errors)})
        return
    elif unchanged(feed_path, xml_text):
        log(f"  = {len(episodes)} 集，无变化")
    else:
        write_text(feed_path, xml_text)
        log(f"  ✓ {len(episodes)} 集 → {args.out}/{feed_rel}（{len(xml_text) // 1024} KB）")

    results.append({
        "slug": slug,
        "pid": pid,
        "title": meta["title"],
        "author": meta["author"],
        "image": meta["image"],
        "count": len(episodes),
        "added": len(new_ids),
        "skipped": max(len(merged) - len(episodes), 0),
        "latest": episodes[0].get("pub_date") if episodes else None,
        "feed": feed_rel,
        "hosting": cfg.get("hosting") or "xyzfm",
        "apple_ok": not errors,
        **({"backfill_error": backfill_error} if backfill_error else {}),
    })


# ───────────────────────────── 索引页 ─────────────────────────────

# 汉字 → 拼音首字母：加载自 pinyin_letters.txt（pypinyin 预生成，26702 字，78KB）
# 运行时纯标准库；表查不到的字符归 #
_PY_TABLE = {}
_PY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pinyin_letters.txt")
try:
    with open(_PY_FILE, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if len(_line) > 2 and _line[1] == ":":
                for _ch in _line[2:]:
                    _PY_TABLE[_ch] = _line[0]
except OSError:
    pass


def first_letter(title):
    ch = (title or "").strip()[:1]
    if not ch:
        return "#"
    if ch.isascii():
        return ch.upper() if ch.isalpha() else "#"
    return _PY_TABLE.get(ch, "#")

INDEX = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dayuzhou</title>
<style>
  :root {{ --ink:#e9e9e9; --dim:#9d9d9d; --bg:#0b0b0b; --line:#3a3a3a;
           --red:#ff3b3b; --green:#4dff6e; }}
  * {{ box-sizing:border-box; }}
  html {{ background:var(--bg); }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.55 "Courier New",ui-monospace,SFMono-Regular,Menlo,monospace;
    -webkit-font-smoothing:none; letter-spacing:.3px;
  }}
  body::after {{
    content:""; position:fixed; inset:0; pointer-events:none; z-index:9;
    background:repeating-linear-gradient(180deg, rgba(255,255,255,.035) 0 1px, transparent 1px 3px);
  }}
  .wrap {{ max-width:780px; margin:0 auto; padding:36px 16px 64px; position:relative; z-index:1; }}
  #galaxy {{
    position:fixed; left:50%; top:50%; width:1150px; height:1150px;
    transform:translate(-50%,-50%); z-index:0; pointer-events:none;
    image-rendering:pixelated; opacity:.42;
    filter:grayscale(.92) brightness(.45) saturate(.1);
    transition:filter 1.6s ease;
    animation:spin 240s linear infinite;
  }}
  #galaxy.flare {{
    filter:grayscale(0) brightness(1.08) saturate(1.25);
    transition:filter .3s ease;
  }}
  @keyframes spin {{ to {{ transform:translate(-50%,-50%) rotate(360deg); }} }}
  @media (prefers-reduced-motion: reduce) {{ #galaxy {{ animation:none; }} }}

  /* ── 头部：霓虹灯牌 403 → 200 ── */
  .hdr {{
    border:3px solid var(--ink); padding:18px 16px 16px; margin-bottom:14px;
    text-align:center; position:relative; overflow:hidden;
    box-shadow:inset 0 0 40px rgba(0,0,0,.55);
    clip-path:polygon(
      0 12px, 6px 12px, 6px 6px, 12px 6px, 12px 0,
      calc(100% - 12px) 0, calc(100% - 12px) 6px, calc(100% - 6px) 6px, calc(100% - 6px) 12px, 100% 12px,
      100% calc(100% - 12px), calc(100% - 6px) calc(100% - 12px), calc(100% - 6px) calc(100% - 6px),
      calc(100% - 12px) calc(100% - 6px), calc(100% - 12px) 100%,
      12px 100%, 12px calc(100% - 6px), 6px calc(100% - 6px), 6px calc(100% - 12px), 0 calc(100% - 12px)
    );
  }}
  .hdr::before {{
    content:""; position:absolute; inset:0; pointer-events:none;
    background:repeating-linear-gradient(180deg, rgba(255,255,255,.02) 0 1px, transparent 1px 3px);
  }}
  .sign {{ display:flex; align-items:center; justify-content:center; gap:12px; margin:0 0 8px; }}
  .code {{
    display:inline-block; font-size:28px; font-weight:700; letter-spacing:3px;
    padding:1px 10px; border:2px solid currentColor; line-height:1.3;
  }}
  .bad {{
    color:var(--red);
    text-shadow:0 0 4px var(--red), 0 0 14px #ff0000, 0 0 30px rgba(255,0,0,.55);
    box-shadow:0 0 9px rgba(255,40,40,.5), inset 0 0 9px rgba(255,40,40,.28);
    animation:blink 1.2s steps(1) infinite;
  }}
  .ok {{
    color:var(--green);
    text-shadow:0 0 4px var(--green), 0 0 16px #00ff41, 0 0 34px rgba(0,255,65,.5);
    box-shadow:0 0 11px rgba(60,255,110,.45), inset 0 0 9px rgba(60,255,110,.22);
    animation:hum 2.4s ease-in-out infinite;
  }}
  .to {{ color:#3d3d3d; font-size:16px; letter-spacing:4px; font-weight:700; }}
  @keyframes blink {{
    0%,71% {{ opacity:1; }}
    75% {{ opacity:.22; }}
    79% {{ opacity:1; }}
    85% {{ opacity:.38; }}
    89%,100% {{ opacity:1; }}
  }}
  @keyframes hum {{
    0%,100% {{ opacity:.62; }}
    50% {{ opacity:1; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ .bad, .ok {{ animation:none; }} }}
  @media (max-width:560px) {{ .code {{ font-size:20px; }} .to {{ font-size:13px; }} }}

  h1 {{ font-size:22px; margin:0 0 6px; letter-spacing:6px; font-weight:700; text-transform:uppercase; }}
  .sub {{ color:var(--dim); font-size:12px; letter-spacing:1px; text-transform:uppercase; }}

  .bar {{ display:flex; gap:8px; margin-bottom:16px; }}
  .bar span {{ color:var(--ink); padding-top:7px; }}
  .search {{
    flex:1; padding:7px 10px; background:#141414; color:var(--ink);
    border:2px solid var(--line); font:13px/1.4 "Courier New",monospace;
    letter-spacing:.5px; outline:none; border-radius:0;
  }}
  .search:focus {{ border-color:var(--ink); }}
  .item {{
    display:flex; align-items:center; gap:10px; padding:8px 10px;
    border-bottom:1px dashed var(--line);
  }}
  .item:hover {{ background:#161616; box-shadow:inset 3px 0 0 var(--ink); }}
  .no {{ width:34px; color:var(--dim); font-size:12px; flex:0 0 auto; }}
  .cov {{
    width:26px; height:26px; flex:0 0 auto; object-fit:cover;
    filter:grayscale(1) contrast(1.25); image-rendering:pixelated;
    border:1px solid var(--line); background:#1c1c1c;
    transition:filter .25s steps(4);
  }}
  .item:hover .cov {{ filter:none; }}
  .nm {{
    flex:0 0 190px; min-width:0; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; font-size:13px;
  }}
  .u {{ flex:1; min-width:0; }}
  .u input {{
    width:100%; background:transparent; color:#b9b9b9; border:0;
    font:12px/1.4 "Courier New",monospace; padding:2px 0; outline:none;
  }}
  .u input:focus {{ color:var(--ink); }}
  button {{
    flex:0 0 auto; background:transparent; color:var(--ink);
    border:2px solid var(--line); padding:4px 9px; cursor:pointer;
    font:11px/1.2 "Courier New",monospace; letter-spacing:1px; border-radius:0;
    text-transform:uppercase;
  }}
  button:hover {{ border-color:var(--ink); background:var(--ink); color:var(--bg); }}
  .sec {{
    display:flex; align-items:center; gap:10px; padding:18px 10px 6px;
    color:var(--ink); font-size:14px; font-weight:700; letter-spacing:2px;
  }}
  .sec::after {{ content:""; flex:1; border-top:1px dashed var(--line); }}
  .empty {{ display:none; color:var(--dim); padding:22px 10px; letter-spacing:2px; }}
  .foot {{ margin-top:26px; color:var(--dim); font-size:11px; letter-spacing:1px; text-align:center; }}
  @media (max-width:640px) {{
    .wrap {{ padding:24px 10px 48px; }}
    .item {{ flex-wrap:wrap; padding:10px 8px; gap:8px; }}
    .no {{ display:none; }}
    .nm {{ flex:1 0 calc(100% - 48px); font-size:12.5px; }}
    .u {{ flex:1 1 58%; min-width:0; padding:6px 0; }}
    .u input {{ font-size:11px; }}
    .cov {{ width:32px; height:32px; }}
    button {{ padding:9px 12px; font-size:12px; }}
    .search {{ font-size:16px; }}
    .code {{ font-size:18px; padding:1px 8px; }}
    .to {{ font-size:12px; }}
    h1 {{ font-size:18px; letter-spacing:4px; }}
    .sec {{ padding-top:14px; }}
  }}
  @media (hover: none) {{
    .item:active {{ background:#1c1c1c; }}
    .item:active .cov {{ filter:none; }}
  }}
  body {{ -webkit-tap-highlight-color:transparent; }}
</style>
</head>
<body>
<canvas id="galaxy" width="150" height="150"></canvas>
<div class="wrap">
  <div class="hdr">
    <div class="sign">
      <span class="code bad">403</span>
      <span class="to">TO</span>
      <span class="code ok">200</span>
    </div>
    <h1>dayuzhou</h1>
    <div class="sub">{count} feeds / {total} eps / {updated}</div>
  </div>
  <div class="bar"><span>&gt;</span><input class="search" id="q" placeholder="filter" oninput="filterList(this.value)"></div>
  <div id="list">
{cards}
    <div class="empty" id="empty">&gt;&gt;&gt; NO MATCH &lt;&lt;&lt;</div>
  </div>
  <div class="foot">real podcasts have open rss — this page exists to keep it that way</div>
</div>
<script>
(function () {{
  var c = document.getElementById('galaxy'), g = c.getContext('2d');
  var cx = 75, cy = 75, R = 70, ARMS = 2;
  var i, t, r, ang, arm, x, y, al;
  // 星色板：白 / 淡蓝 / 青紫，核心偏暖白
  var STARS = [
    [235,238,245],[200,216,255],[160,190,255],[186,170,255],[150,225,235],[255,244,214]
  ];
  for (i = 0; i < 3200; i++) {{
    t = i / 3200;
    r = 3 + t * R * 0.92;
    ang = t * Math.PI * 5.6 + (i % ARMS) * (Math.PI * 2 / ARMS);
    x = cx + Math.cos(ang) * r + (Math.random() - 0.5) * t * 10;
    y = cy + Math.sin(ang) * r + (Math.random() - 0.5) * t * 10;
    var c = STARS[Math.floor(Math.random() * STARS.length)];
    al = 0.7 - t * 0.5;
    g.fillStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + al.toFixed(2) + ')';
    g.fillRect(x | 0, y | 0, 1, 1);
  }}
  for (i = 0; i < 240; i++) {{
    x = cx + (Math.random() - 0.5) * 12;
    y = cy + (Math.random() - 0.5) * 12;
    g.fillStyle = 'rgba(255,240,214,' + (0.8 - Math.random() * 0.45).toFixed(2) + ')';
    g.fillRect(x | 0, y | 0, 1, 1);
  }}
  // 少量离散星点
  for (i = 0; i < 60; i++) {{
    x = Math.random() * 150; y = Math.random() * 150;
    g.fillStyle = 'rgba(235,240,255,' + (0.15 + Math.random() * 0.3).toFixed(2) + ')';
    g.fillRect(x | 0, y | 0, 1, 1);
  }}
}})();
function filterList(q) {{
  q = q.trim().toLowerCase();
  var shown = 0;
  document.querySelectorAll('#list .item').forEach(function (el) {{
    var hit = !q || el.dataset.name.toLowerCase().includes(q);
    el.style.display = hit ? '' : 'none';
    if (hit) shown++;
  }});
  document.querySelectorAll('#list .sec').forEach(function (el) {{
    el.style.display = q ? 'none' : '';
  }});
  document.getElementById('empty').style.display = shown ? 'none' : 'block';
}}
var flareTimer = null;
function galaxyFlare() {{
  var g = document.getElementById('galaxy');
  if (!g) return;
  g.classList.add('flare');
  if (flareTimer) clearTimeout(flareTimer);
  flareTimer = setTimeout(function () {{ g.classList.remove('flare'); }}, 1500);
}}
// 订阅地址统一补成绝对路径：复制出来直接能粘进播客 App
document.querySelectorAll('#list input').forEach(function (el) {{
  if (el.value && el.value.indexOf('http') !== 0) {{
    try {{ el.value = new URL(el.value, location.href).href; }} catch (e) {{}}
  }}
}});
document.querySelectorAll('button[data-copy]').forEach(function (b) {{
  b.addEventListener('click', function () {{
    galaxyFlare();
    var el = document.getElementById(b.dataset.copy);
    el.select();
    navigator.clipboard.writeText(el.value).then(function () {{
      var t = b.textContent; b.textContent = 'OK';
      setTimeout(function () {{ b.textContent = t; }}, 1000);
    }});
  }});
}});
</script>
</body>
</html>
"""


def write_index(out_dir, results, base_url):
    ok = [r for r in results if not r.get("error")]
    index_path = os.path.join(out_dir, "index.html")
    json_path = os.path.join(out_dir, "feeds.json")

    try:
        with open(json_path, encoding="utf-8") as f:
            old_feeds = json.load(f).get("feeds")
    except (OSError, ValueError):
        old_feeds = None
    if old_feeds == results and os.path.exists(index_path):
        log("索引无变化，跳过写入")
        return

    cards = []
    ok_sorted = sorted(
        ok,
        key=lambda r: (first_letter(r.get("title") or ""), -(r.get("subs") or 0)),
    )
    idx = 0
    prev_letter = None
    for r in ok_sorted:
        letter = first_letter(r.get("title") or "")
        if letter != prev_letter:
            cards.append(f'    <div class="sec">{letter}</div>')
            prev_letter = letter
        url = f"{base_url.rstrip('/')}/{r['feed']}" if base_url else r["feed"]
        idx += 1
        cards.append(f"""    <div class="item" data-name="{esc(r['title'])}">
      <div class="no">{idx:03d}</div>
      <img class="cov" src="{esc(r.get('image') or '')}" alt="" loading="lazy">
      <div class="nm" title="{esc(r['title'])}">{esc(r['title'])}</div>
      <div class="u"><input id="f{idx - 1}" value="{esc(url)}" readonly onclick="this.select()"></div>
      <button data-copy="f{idx - 1}">copy</button>
    </div>""")
    now = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    write_text(
        index_path,
        INDEX.format(count=len(ok), total=sum(x["count"] for x in ok), updated=now,
                     cards="\n".join(cards)),
    )
    write_text(
        json_path,
        json.dumps({"updated_at": now, "feeds": results}, ensure_ascii=False, indent=2) + "\n",
    )
    write_text(os.path.join(out_dir, ".nojekyll"), "")


# ───────────────────────────── 入口 ─────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="dayuzhou · 从页面数据重建小宇宙播客 RSS")
    ap.add_argument("--config", help="播客清单（默认 config/podcasts.json）")
    ap.add_argument("--pid", help="只跑这一个播客（链接或 pid）")
    ap.add_argument("--slug", help="配合 --pid 的输出文件名")
    ap.add_argument("--out", default="docs", help="输出目录（默认 docs）")
    ap.add_argument("--limit", type=int, default=0, help="每个播客最多输出多少集")
    ap.add_argument("--base-url", default="", help="站点根地址（atom:self 与订阅链接）")
    ap.add_argument("--backfill", choices=["auto", "always", "never"], default="auto",
                    help="auto=首次自动回填（默认）；always=强制回填；never=只取最近 15 集")
    ap.add_argument("--token-file", help="refresh token 文件")
    ap.add_argument("--refresh-token", help="直接给 refresh token")
    ap.add_argument("--access-token", help="直接给 access token")
    ap.add_argument("--token-is-access", action="store_true", help="--token-file 里是 access token")
    ap.add_argument("--device-id", help="x-jike-device-id，缺省时按 token 派生一个固定值")
    ap.add_argument("--no-token", action="store_true", help="不读任何凭证")
    ap.add_argument("--workers", type=int, default=5, help="单播客并发（默认 5）")
    ap.add_argument("--podcast-workers", type=int, default=3, help="同时处理几个播客（默认 3）")
    ap.add_argument("--rate", type=float, default=12, help="对小宇宙的全局请求速率（次/秒，默认 12）")
    ap.add_argument("--strict", action="store_true", help="Apple 规范有硬性错误时不写文件")
    ap.add_argument("--dry-run", action="store_true", help="不写文件")
    ap.add_argument("--slice", metavar="I/N", help="只处理第 I 份（共 N 份），如 2/5；用于分批提交")
    ap.add_argument("--index-only", action="store_true",
                    help="不抓取，只根据 docs/feeds 下已有的 XML 和配置重建索引页")
    args = ap.parse_args()
    LIMITER.set_rate(args.rate)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not args.config and not args.pid:
        args.config = os.path.join(root, "config", "podcasts.json")

    podcasts = []
    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            conf = json.load(f)
        podcasts = conf.get("podcasts") or []
        args.base_url = args.base_url or conf.get("base_url") or ""
        owner = conf.get("owner") or {}
        for p in podcasts:
            p.setdefault("owner_name", owner.get("name", ""))
            p.setdefault("owner_email", owner.get("email", ""))
    if args.pid:
        podcasts = [{"pid": args.pid, "slug": args.slug or ""}] + [
            p for p in podcasts if parse_pid(p["pid"]) != parse_pid(args.pid)
        ]
    if not podcasts:
        ap.error("没有要处理的播客：检查 config/podcasts.json")

    # --slice I/N：只处理第 I 份，配合工作流分批提交，最坏只丢一个批次
    if args.slice:
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", args.slice.strip())
        if not m or not (1 <= int(m.group(1)) <= int(m.group(2))) or int(m.group(2)) < 1:
            ap.error(f"--slice 格式应为 I/N 且 1<=I<=N，收到 {args.slice!r}")
        i, n = int(m.group(1)), int(m.group(2))
        podcasts = podcasts[i - 1::n]
        log(f"切片 {i}/{n}：本批 {len(podcasts)} 个")

    if not args.base_url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            args.base_url = f"https://{owner.lower()}.github.io/{name}"

    if args.index_only:
        by_slug = {p["slug"]: p for p in podcasts}
        results = []
        for path in sorted(glob.glob(os.path.join(args.out, "feeds", "*.xml"))):
            slug = os.path.basename(path)[:-4]
            cfg = by_slug.get(slug)
            if not cfg:
                continue
            try:
                eps = read_feed(path)
            except Exception as e:
                warn(f"! 旧 RSS 损坏，跳过：{slug}（{e}）")
                continue
            if not eps:
                continue
            results.append({
                "slug": slug, "pid": cfg["pid"], "title": cfg.get("title") or "",
                "author": cfg.get("author") or "", "image": eps[0].get("image") or "",
                "count": len(eps), "added": 0, "skipped": 0,
                "latest": eps[0].get("pub_date"), "feed": f"feeds/{slug}.xml",
                "hosting": cfg.get("hosting") or "xyzfm", "apple_ok": True,
            })
        write_index(args.out, results, args.base_url)
        log(f"索引重建完成：{len(results)} 个 feed")
        return

    cred = Credential.load(args)
    log(f"dayuzhou · {len(podcasts)} 个播客 · 回填策略 {args.backfill}"
        f" · {'有 token' if cred else '无 token（首次只能取最近 15 集）'}")

    site = Site()
    results = []

    def safe(cfg):
        local = []
        try:
            build(cfg, site, cred, args, local)
        except Exception as e:
            warn(f"✗ {cfg.get('pid')}: {e}")
            local.append({"slug": cfg.get("slug") or "", "pid": cfg.get("pid"),
                          "title": cfg.get("title") or "", "error": str(e)})
        return local

    if args.podcast_workers <= 1:
        for cfg in podcasts:
            results.extend(safe(cfg))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.podcast_workers) as pool:
            for chunk in pool.map(safe, podcasts):
                results.extend(chunk)

    ok = [r for r in results if not r.get("error")]
    if not args.dry_run:
        write_index(args.out, results, args.base_url)
    log(f"\n✓ {len(ok)}/{len(results)} 个播客 · {sum(r.get('count', 0) for r in ok)} 集")
    if any(r.get("error") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
