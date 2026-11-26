#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xyz
"""
import argparse
import datetime as dt
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dayuzhou as dz  # noqa: E402

STATE = os.path.join(ROOT, "state", "backfill.json")
GIVE_UP_AFTER = 3

AUTH_HINTS = ("401", "403", "token", "session", "凭证", "续期", "失效", "未登录", "被拒绝")


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("done", {})
    state.setdefault("skipped", {})
    state.setdefault("fails", {})
    state.setdefault("completed", None)
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def base_url_of(conf, cli_value):
    url = (cli_value or conf.get("base_url") or "").strip()
    if url:
        return url
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


def pick_credential(cli):
    """refresh token 优先（长期可用、自动轮换）；access token 只用于临时调试。"""
    at = (cli.access_token or "").strip() or os.environ.get("XYZ_ACCESS_TOKEN", "").strip()
    rt = (cli.refresh_token or "").strip() or os.environ.get("XYZ_REFRESH_TOKEN", "").strip()
    path = cli.token_file or os.path.join(ROOT, "config", "token.txt")
    if os.path.exists(os.path.expanduser(path)):
        value = open(os.path.expanduser(path), encoding="utf-8").read().strip()
        if cli.token_is_access:
            at = at or value
        else:
            rt = rt or value
    if rt:
        return dz.Credential(refresh_token=rt, device_id=cli.device_id, token_path=path)
    if at:
        return dz.Credential(access_token=at, device_id=cli.device_id, token_path=path)
    return None


def looks_auth_error(message):
    low = (message or "").lower()
    return any(h in low for h in AUTH_HINTS)


def main():
    ap = argparse.ArgumentParser(description="回填播客的全部历史（session 或 refresh token）")
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "podcasts.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs"))
    ap.add_argument("--count", type=int, default=0, help="本次补几个；0 = 随机 min~max")
    ap.add_argument("--min", type=int, default=10, help="随机下限（默认 10）")
    ap.add_argument("--max", type=int, default=20, help="随机上限（默认 20）")
    ap.add_argument("--access-token", help="x-jike-access-token（session），也可用环境变量 XYZ_ACCESS_TOKEN")
    ap.add_argument("--refresh-token", help="x-jike-refresh-token，也可用环境变量 XYZ_REFRESH_TOKEN")
    ap.add_argument("--token-file", help="从文件读 token（默认按 refresh，加 --token-is-access 按 session）")
    ap.add_argument("--token-is-access", action="store_true", help="--token-file 里是 access token")
    ap.add_argument("--device-id", help="x-jike-device-id，缺省时按 token 派生")
    ap.add_argument("--base-url", default="", help="站点根地址，用于 feed 里的 atom:self")
    ap.add_argument("--rate", type=float, default=2.5, help="请求速率（次/秒），越低越像真人")
    ap.add_argument("--think", default="25-90", help="播客之间的停顿秒数范围，如 25-90")
    ap.add_argument("--start-delay", type=int, default=240, help="启动随机延迟上限（秒）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不抓取")
    cli = ap.parse_args()

    state = load_state()
    with open(cli.config, encoding="utf-8") as f:
        conf = json.load(f)
    podcasts = conf.get("podcasts") or []
    done, skipped, fails = state["done"], state["skipped"], state["fails"]
    remaining = [
        p for p in podcasts
        if p.get("slug") and p["slug"] not in done and p["slug"] not in skipped
    ]
    total = len(podcasts)
    print(f"总进度：{len(done)}/{total} 已回填，{len(skipped)} 个已放弃，剩 {len(remaining)}")

    if not remaining:
        print("ALL DONE")
        if not state.get("completed"):
            state["completed"] = dt.date.today().isoformat()
            save_state(state)
        return 0

    cred = pick_credential(cli)
    if not cred and not cli.dry_run:
        print("没有凭证：把 refresh token 写进 config/token.txt，或设 XYZ_REFRESH_TOKEN",
              file=sys.stderr)
        return 2
    mode = "未配置" if not cred else (
        "session" if (cred.access_token and not cred.refresh_token) else "refresh token")
    print(f"凭证类型：{mode}")

    delay = random.randint(0, cli.start_delay)
    print(f"随机延迟 {delay}s 后开始")
    if not cli.dry_run:
        time.sleep(delay)

    k = cli.count or random.randint(cli.min, cli.max)
    k = min(k, len(remaining))
    picks = random.sample(remaining, k)
    print(f"本次目标 {k} 个：" + "、".join(p.get("title", "")[:14] for p in picks))

    if cli.dry_run:
        print("（dry-run：只展示计划，不抓取）")
        return 0

    ns = argparse.Namespace(
        out=cli.out, backfill="always", workers=3, limit=0,
        base_url=base_url_of(conf, cli.base_url), dry_run=False, strict=False,
    )
    if not ns.base_url:
        print("提示：没给 --base-url，feed 里不会写 atom:self（不影响播放，但订阅地址建议用线上完整 URL）")

    dz.LIMITER.set_rate(cli.rate)
    site = dz.Site()
    lo, hi = (int(x) for x in cli.think.split("-"))
    ok = failed = 0
    auth_streak = 0
    t_start = time.time()

    for idx, p in enumerate(picks, 1):
        slug = p["slug"]
        print(f"\n[{idx}/{k}] {p.get('title', '')}（{slug}）")
        results = []
        try:
            dz.build(p, site, cred, ns, results)
            r = results[0] if results else {}
            if r.get("error"):
                raise RuntimeError(r["error"])
            if r.get("backfill_error"):
                raise RuntimeError("回填未完成：" + r["backfill_error"])
            done[slug] = {
                "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "eps": r.get("count", 0),
            }
            fails.pop(slug, None)
            save_state(state)
            ok += 1
            auth_streak = 0
        except Exception as e:
            msg = str(e)
            print(f"  ✗ 失败：{msg}", file=sys.stderr)
            failed += 1
            fails[slug] = int(fails.get(slug, 0)) + 1
            if fails[slug] >= GIVE_UP_AFTER:
                skipped[slug] = {"at": dt.datetime.now().strftime("%Y-%m-%d"), "reason": msg[:200]}
                print(f"  · 累计失败 {GIVE_UP_AFTER} 次，标记放弃，不再重试")
            save_state(state)
            if looks_auth_error(msg):
                auth_streak += 1
                if auth_streak >= 3:
                    print("\n连续 3 个都像凭证/风控问题（401/403/token），提前停止。"
                          "换个新 session 再跑，已完成的不会重做。", file=sys.stderr)
                    break
            else:
                auth_streak = 0
        if idx < k:
            pause = random.uniform(lo, hi)
            print(f"  …停顿 {pause:.0f}s")
            time.sleep(pause)

    cost = time.time() - t_start
    left = total - len(done) - len(skipped)
    print(f"\n本次完成 {ok}，失败 {failed}，累计 {len(done)}/{total}，"
          f"剩 {left}，用时 {cost / 60:.1f} 分钟")
    if len(done) + len(skipped) >= total:
        state["completed"] = dt.date.today().isoformat()
        save_state(state)
        print("ALL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
