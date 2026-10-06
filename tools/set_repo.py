#!/usr/bin/env python3
"""
把仓库里的占位坐标一次性改成你的真实 GitHub 仓库。

发布前必须做这一步：manifest.json 的 documentation / issue_tracker / codeowners
是 HACS 和 hassfest 的校验项，留占位符会直接校验失败。

用法：
    python3 tools/set_repo.py <owner>/<repo>            # 例如 zhangsan/hikvision_acs
    python3 tools/set_repo.py <owner>/<repo> --dry-run  # 只看会改什么
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLACEHOLDER_REPO = "hikvision-acs/hikvision_acs"
PLACEHOLDER_OWNER = "hikvision-acs"


def main() -> int:
    ap = argparse.ArgumentParser(description="设置仓库坐标")
    ap.add_argument("repo", help="形如 owner/repo")
    ap.add_argument("--dry-run", action="store_true", help="只显示改动，不写文件")
    args = ap.parse_args()

    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        print(f"仓库坐标格式不对：{args.repo}（应形如 owner/repo）", file=sys.stderr)
        return 2
    owner, _ = args.repo.split("/", 1)

    # (文件, 原串, 新串, 说明)
    edits: list[tuple[pathlib.Path, str, str, str]] = [
        (ROOT / "custom_components/hikvision_acs/manifest.json",
         PLACEHOLDER_REPO, args.repo, "manifest.documentation / issue_tracker"),
        (ROOT / "custom_components/hikvision_acs/manifest.json",
         f'"@{PLACEHOLDER_OWNER}"', f'"@{owner}"', "manifest.codeowners"),
        (ROOT / "blueprints/automation/hikvision_acs/doorbell_popup.yaml",
         PLACEHOLDER_REPO, args.repo, "蓝图 source_url"),
        (ROOT / "LICENSE",
         f"Copyright (c) 2026 {PLACEHOLDER_OWNER}", f"Copyright (c) 2026 {owner}",
         "LICENSE 版权人"),
    ]

    changed = 0
    for path, old, new, label in edits:
        if not path.is_file():
            print(f"  ⏭  跳过（文件不存在）：{path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        if old not in text:
            print(f"  ⏭  {label}：没找到旧值，可能已改过")
            continue
        count = text.count(old)
        if not args.dry_run:
            path.write_text(text.replace(old, new), encoding="utf-8")
        print(f"  {'(试运行) ' if args.dry_run else ''}✅ {label}"
              f"  {old} -> {new}  ({count} 处)"
              f"  [{path.relative_to(ROOT)}]")
        changed += 1

    # manifest.json 改完必须仍是合法 JSON——HACS 校验会因为一个逗号全盘失败
    manifest = ROOT / "custom_components/hikvision_acs/manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        print(f"\n❌ manifest.json 已损坏：{err}", file=sys.stderr)
        return 1

    print()
    print("当前 manifest.json：")
    for key in ("codeowners", "documentation", "issue_tracker"):
        print(f"  {key}: {data.get(key)}")

    # 提醒 CHANGELOG 里的发布链接也要改（格式特殊，不自动改以免误伤）
    changelog = (ROOT / "CHANGELOG.md")
    if changelog.is_file() and PLACEHOLDER_REPO in changelog.read_text(encoding="utf-8"):
        print(f"\n⚠️  CHANGELOG.md 末尾的发布链接仍指向占位仓库，请手工改成 "
              f"https://github.com/{args.repo}/releases/tag/v0.1.0")

    print(f"\n{'（试运行，未写入）' if args.dry_run else f'已更新 {changed} 处'}")
    print("改完记得跑一遍：python3 tools/validate_integration.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
