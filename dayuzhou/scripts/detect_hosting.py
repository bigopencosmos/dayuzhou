#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_hosting.py — 判断一个播客的 RSS 是不是托管在小宇宙（xyzfm）上

原理：Apple Podcasts 的 feedUrl 会暴露托管商。
    小宇宙托管的节目，feedUrl 形如 https://feed.xyzfm.space/<slug>
    音频则指向 media.xyzcdn.net

用法：
    python3 scripts/detect_hosting.py 岩中花述 忽左忽右
    python3 scripts/detect_hosting.py --file names.txt --json out.json
    python3 scripts/detect_hosting.py --from-config --check

    --check  真的去拉一次 feed，报告可达性 / 集数 / 音频域名
             （xyzfm 的 feed 目前对非白名单 IP 返回 403，这本身就是问题所在）
"""

import argparse
import concurrent.futures
import difflib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ITUNES = "https://itunes.apple.com/search"
UA_CLIENT = "AppleCoreMedia/1.0.0.21A360 (iPhone; U; CPU OS 17_0 like Mac OS X; zh_cn)"
UA_WEB = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

_THROTTLE_LOCK = threading.Lock()
_LAST_CALL = [0.0]


def _throttle(min_gap=0.7):
    """iTunes 大约 20 次/分钟，这里统一压一下节奏。"""
    with _THROTTLE_LOCK:
        wait = min_gap - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA_WEB, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers, r.read()


def itunes_search(term, country="cn", limit=5):
    q = urllib.parse.urlencode({"term": term, "entity": "podcast", "country": country, "limit": limit})
    _, _, body = get(f"{ITUNES}?{q}")
    return json.loads(body).get("results", [])


def best_match(term, results):
    if not results:
        return None
    scored = []
    for r in results:
        name = r.get("collectionName") or ""
        score = difflib.SequenceMatcher(None, term.lower(), name.lower()).ratio()
        if term.lower() in name.lower() or name.lower() in term.lower():
            score = max(score, 0.9)
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    score, pick = scored[0]
    pick["_score"] = round(score, 2)
    return pick


def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_xyz(host):
    return "xyzfm" in host or host.endswith("xyzcdn.net") or "xiaoyuzhou" in host


def probe_feed(url):
    """拉一次 feed，判断是否可用、共几集、音频托管在哪。"""
    info = {"reachable": False, "status": None, "items": 0, "audio_host": "", "note": ""}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA_CLIENT, "Accept": "application/rss+xml,*/*"})
        with urllib.request.urlopen(req, timeout=25) as r:
            info["status"] = r.status
            body = r.read()
            ctype = r.headers.get("Content-Type", "")
            info["note"] = ctype.split(";")[0]
    except urllib.error.HTTPError as e:
        info["status"] = e.code
        info["note"] = f"HTTP {e.code}"
        return info
    except Exception as e:
        info["note"] = type(e).__name__
        return info

    if b"<rss" not in body[:2000] and b"<feed" not in body[:2000]:
        info["note"] = "不是 RSS（多半被拦到验证页）"
        return info

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        info["note"] = "XML 解析失败"
        return info
    items = root.findall("./channel/item")
    info["items"] = len(items)
    info["reachable"] = True
    for it in items:
        enc = it.find("enclosure")
        if enc is not None:
            info["audio_host"] = host_of(enc.get("url") or "")
            break
    return info


def check_one(name, country, do_check):
    out = {"query": name, "matched": "", "artist": "", "feed": "", "feed_host": "",
           "xyz_hosted": False, "score": 0, "collection_id": "", "probe": None}
    try:
        _throttle()
        results = itunes_search(name, country=country)
    except Exception as e:
        out["error"] = f"iTunes 请求失败：{e}"
        return out
    pick = best_match(name, results)
    if not pick:
        out["error"] = "Apple Podcasts 没有收录"
        return out

    out["matched"] = pick.get("collectionName", "")
    out["artist"] = pick.get("artistName", "")
    out["feed"] = pick.get("feedUrl", "")
    out["feed_host"] = host_of(out["feed"])
    out["xyz_hosted"] = is_xyz(out["feed_host"])
    out["score"] = pick.get("_score", 0)
    out["collection_id"] = pick.get("collectionId", "")
    # Apple 的搜索结果经常跑偏，低分匹配要显式标出来，别让人误信
    out["low_confidence"] = out["score"] < 0.5

    if do_check and out["feed"]:
        out["probe"] = probe_feed(out["feed"])
        if not out["xyz_hosted"] and out["probe"]["audio_host"]:
            # feed 不在 xyz，但音频在 xyzcdn，也算小宇宙托管
            out["audio_xyz"] = is_xyz(out["probe"]["audio_host"])
    return out


def load_config_pids(path):
    with open(path, encoding="utf-8") as f:
        conf = json.load(f)
    return [p.get("title") or p.get("pid") for p in conf.get("podcasts", [])]


def main():
    ap = argparse.ArgumentParser(description="判断播客 RSS 是否托管在小宇宙（xyzfm）")
    ap.add_argument("names", nargs="*", help="播客名称")
    ap.add_argument("--file", help="每行一个播客名的文本文件")
    ap.add_argument("--from-config", nargs="?", const="config/podcasts.json",
                    help="从 podcasts.json 读取标题")
    ap.add_argument("--country", default="cn", help="iTunes 地区（默认 cn）")
    ap.add_argument("--check", action="store_true", help="额外拉取 feed，检查可达性/集数/音频域名")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    ap.add_argument("--workers", type=int, default=3, help="并发（默认 3，iTunes 有频率限制）")
    args = ap.parse_args()

    names = list(args.names)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            names += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if args.from_config:
        names += load_config_pids(args.from_config)
    if not names:
        ap.error("给几个播客名，或用 --file / --from-config")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(lambda n: check_one(n, args.country, args.check), names):
            results.append(r)

    # 表格输出
    print(f"{'查询':<24} {'匹配到的节目':<28} {'托管':<9} {'feed'}")
    print("-" * 112)
    for r in results:
        if r.get("error"):
            print(f"{r['query'][:22]:<24} {'—':<28} {'?':<9} {r['error']}")
            continue
        host = r["feed_host"]
        tag = "xyzfm" if r["xyz_hosted"] else (host or "?")
        if r.get("audio_xyz"):
            tag += "+xyzcdn"
        if r.get("low_confidence"):
            tag = "?" + tag
        print(f"{r['query'][:22]:<24} {r['matched'][:26]:<28} {tag[:14]:<9} {r['feed']}")
        if r.get("probe"):
            p = r["probe"]
            state = f"{p['items']} 集" if p["reachable"] else f"不可用（{p['note']}）"
            print(f"{'':<24} └─ {state}" + (f"｜音频 {p['audio_host']}" if p.get("audio_host") else ""))

    xyz = [r for r in results if r.get("xyz_hosted") or r.get("audio_xyz")]
    xyz_bad = [r for r in xyz if r.get("probe") and not r["probe"]["reachable"]]
    unsure = [r for r in results if r.get("low_confidence")]
    summary = f"\n合计 {len(results)} 个｜小宇宙托管 {len(xyz)} 个"
    if any(r.get("probe") for r in results):
        summary += f"（其中 {len(xyz_bad)} 个当前拉不到）"
    summary += f"｜匹配不确定 {len(unsure)} 个"
    print(summary)
    if unsure:
        print("  带 ? 的行是 Apple 搜索跑偏的结果，请用更精确的节目名重试")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"明细已写入 {args.json_out}")


if __name__ == "__main__":
    main()
