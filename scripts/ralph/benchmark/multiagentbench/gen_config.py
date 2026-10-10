#!/usr/bin/env python3
"""从 MARBLE 的 coding_config.yaml 生成一份可跑的配置（覆盖模型 / 评估模型 / 迭代数 / 拓扑）。

用法：
    python gen_config.py --out /tmp/ocgo.yaml \
        --model deepseek-v4.1-flash --eval-model deepseek-v4.1-flash \
        --max-iterations 2 --coordinate-mode graph
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="marble/configs/coding_config/coding_config.yaml")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    ap.add_argument("--eval-model", default="deepseek-v4.1-flash")
    ap.add_argument("--max-iterations", type=int, default=2)
    ap.add_argument("--coordinate-mode", default=None)
    args = ap.parse_args()

    data = yaml.safe_load(Path(args.base).read_text(encoding="utf-8"))
    data["llm"] = args.model
    data.setdefault("metrics", {})["evaluate_llm"] = args.eval_model
    data.setdefault("environment", {})["max_iterations"] = args.max_iterations
    if args.coordinate_mode:
        data["coordinate_mode"] = args.coordinate_mode
    Path(args.out).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"wrote {args.out} (model={args.model}, eval={args.eval_model}, "
          f"iters={args.max_iterations}, mode={data.get('coordinate_mode')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
