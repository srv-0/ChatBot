#!/usr/bin/env python3
"""
Generates submission.jsonl (challenge-brief.md §7.2) by running the standalone
compose() function over the 30 canonical (merchant, trigger[, customer]) test
pairs in dataset/test_pairs.json.

Usage:
    python gen_submission.py --dataset ./dataset --out submission.jsonl
"""
import argparse
import json
from pathlib import Path

import bot  # exposes compose()


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="dataset")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    ds = Path(args.dataset)
    test_pairs = load_json(ds / "test_pairs.json")["pairs"]

    categories: dict[str, dict] = {}
    merchants: dict[str, dict] = {}
    customers: dict[str, dict] = {}
    triggers: dict[str, dict] = {}

    for f in (ds / "categories").glob("*.json"):
        c = load_json(f)
        categories[c["slug"]] = c
    for f in (ds / "merchants").glob("*.json"):
        m = load_json(f)
        merchants[m["merchant_id"]] = m
    for f in (ds / "customers").glob("*.json"):
        c = load_json(f)
        customers[c["customer_id"]] = c
    for f in (ds / "triggers").glob("*.json"):
        t = load_json(f)
        triggers[t["id"]] = t

    print(f"Loaded {len(categories)} categories, {len(merchants)} merchants, "
          f"{len(customers)} customers, {len(triggers)} triggers, {len(test_pairs)} test pairs")

    lines = []
    for pair in test_pairs:
        test_id = pair["test_id"]
        trigger = triggers.get(pair["trigger_id"])
        merchant = merchants.get(pair["merchant_id"])
        customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
        if trigger is None or merchant is None:
            print(f"  ! {test_id}: missing trigger/merchant, skipping")
            continue
        category = categories.get(merchant["category_slug"])
        if category is None:
            print(f"  ! {test_id}: missing category {merchant['category_slug']}, skipping")
            continue

        result = bot.compose(category, merchant, trigger, customer)
        line = {
            "test_id": test_id,
            "body": result["body"],
            "cta": result["cta"],
            "send_as": result["send_as"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        }
        lines.append(line)
        print(f"  {test_id} [{trigger.get('kind')}] -> {result['body'][:90]}...")

    with open(args.out, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()
