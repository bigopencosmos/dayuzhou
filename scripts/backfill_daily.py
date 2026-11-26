#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日随机回填：用 refresh token 每天挑 10-20 个播客，补齐它们的全部历史单集。

设计要点：
- 状态存 state/backfill.json，记录已完成的 slug；支持断点，跑一半被杀也不重复
- 全部完成后输出 ALL DONE，工作流后续步骤自动跳过（每天只剩十几秒的空跑）
- 模拟真人节奏：随机启动延迟 / 随机挑选 / 随机顺序 / 低请求速率 / 播客之间随机停顿
- 复用 dayuzhou.py 的抓取与合并逻辑（上一版 RSS 即存档，逐日累积不丢数据）
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


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": {}, "completed": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def base_url_of(conf):
    url = (conf.get("base_url") or "").strip()
    if url:
        return url
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


def main():
    ap = argparse.ArgumentParser(description="每日随机回填 10-20 个播客的全部历史")
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "podcasts.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs"))
    ap.add_argument("--min", type=int, default=10, help="每天最少补几个（默认 10）")
    ap.add_argument("--max", type=int, default=20, help="每天最多补几个（默认 20）")
    ap.add_argument("--count", type=int, default=0, help="固定数量（覆盖 min/max）")
    ap.add_argument("--rate", type=float, default=2.5, help="请求速率（次/秒），越低越像真人")
    ap.add_argument("--think", default="25-90", help="播客之间的停顿秒数范围，如 25-90")
    ap.add_argument("--start-delay", type=int, default=240, help="启动随机延迟上限（秒）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不抓取")
    cli = ap.parse_args()

    state = load_state()
    with open(cli.config, encoding="utf-8") as f:
        conf = json.load(f)
    podcasts = conf.get("podcasts") or []
    done = state.setdefault("done", {})
    remaining = [p for p in podcasts if p.get("slug") and p["slug"] not in done]
    total = len(podcasts)
    print(f"总进度：{total - len(remaining)}/{total} 已回填，剩 {len(remaining)}")

    if not remaining:
        print("ALL DONE")
        state["completed"] = dt.date.today().isoformat()
        save_state(state)
        return 0

    # 真人感 1：不立刻开始
    delay = random.randint(0, cli.start_delay)
    print(f"随机延迟 {delay}s 后开始")
    if not cli.dry_run:
        time.sleep(delay)

    # 真人感 2：随机数量 + 随机挑选 + 随机顺序
    k = cli.count or random.randint(cli.min, cli.max)
    k = min(k, len(remaining))
    picks = random.sample(remaining, k)
    print(f"今日目标 {k} 个：" + "、".join(p.get("title", "")[:14] for p in picks))

    if cli.dry_run:
        print("（dry-run：只展示计划，不抓取）")
        return 0

    nsf = argparse.Namespace(
        out=cli.out, backfill="always", workers=3, limit=0,
        base_url=base_url_of(conf), dry_run=cli.dry_run, strict=False,
    )
    token = os.environ.get("XYZ_REFRESH_TOKEN", "").strip()
    cred = dz.Credential(token) if token else None
    if not cred and not cli.dry_run:
        print("缺少 XYZ_REFRESH_TOKEN（仓库 Settings → Secrets 里没配）", file=sys.stderr)
        return 2

    dz.LIMITER.set_rate(cli.rate)
    site = dz.Site()
    lo, hi = (int(x) for x in cli.think.split("-"))
    ok = failed = 0
    t_start = time.time()

    for idx, p in enumerate(picks, 1):
        print(f"\n[{idx}/{k}] {p.get('title', '')}（{p['slug']}）")
        results = []
        try:
            dz.build(p, site, cred, nsf, results)  # 内部会翻页拉全集并合并进已有 RSS
            r = results[0] if results else {}
            if r.get("error"):
                raise RuntimeError(r["error"])
            done[p["slug"]] = {
                "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "eps": r.get("count", 0),
            }
            save_state(state)  # 每完成一个就落盘，中途被杀也不重复
            ok += 1
        except Exception as e:  # 网络/风控/解析失败都跳过，下次再来
            print(f"  ✗ 失败：{e}", file=sys.stderr)
            failed += 1
        if idx < k:
            # 真人感 3：播客之间像人一样停一会
            pause = random.uniform(lo, hi)
            print(f"  …停顿 {pause:.0f}s")
            if not cli.dry_run:
                time.sleep(pause)

    cost = time.time() - t_start
    print(f"\n今日完成 {ok}，失败 {failed}，累计 {len(done)}/{total}，用时 {cost / 60:.1f} 分钟")
    if len(done) >= total:
        state["completed"] = dt.date.today().isoformat()
        save_state(state)
        print("ALL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
