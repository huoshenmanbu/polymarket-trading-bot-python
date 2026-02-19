#!/usr/bin/env python3
"""
将 .env.example 中的变量同步到 .env，保留 .env 里已有变量的值。

- 若某变量在 .env 中已存在：保留 .env 中的值（不覆盖）。
- 若某变量只在 .env.example 中存在：写入到 .env，使用 example 中的默认值。
- .env.example 中的注释和结构会尽量保留；仅在 .env 中存在、不在 example 中的变量会追加到文件末尾。

用法（在项目根目录执行）：
  python scripts/sync_env_from_example.py
  python3 scripts/sync_env_from_example.py

  仅预览不写入：
  python scripts/sync_env_from_example.py --dry-run
"""
import re
import sys
from pathlib import Path
from typing import Dict, Optional


# 匹配 .env 中的变量行：KEY = value 或 KEY=value，value 可为引号包裹或裸值
VAR_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
    re.MULTILINE,
)


def parse_env_content(text: str) -> Dict[str, str]:
    """从 .env 文件内容解析出 KEY -> value 的字典（只保留最后一个赋值）。"""
    result: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip()
        m = VAR_LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        # 去掉行尾注释
        if " #" in raw or "\t#" in raw:
            raw = raw.split("#", 1)[0].strip()
        value = _unquote_value(raw)
        result[key] = value
    return result


def _unquote_value(raw: str) -> str:
    """去掉值两侧的单引号或双引号（成对）。"""
    if not raw:
        return ""
    if (raw.startswith("'") and raw.endswith("'")) or (
        raw.startswith('"') and raw.endswith('"')
    ):
        return raw[1:-1]
    return raw


def _format_value(value: str) -> str:
    """格式化供 .env 写入：空或含特殊字符时用引号包裹。"""
    if not value:
        return "''"
    lower = value.lower()
    if lower in ("true", "false") or _is_numeric(value):
        return value
    if any(c in value for c in " \t\n#\"'="):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if "'" in value or " " in value:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return value


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def extract_key_from_example_line(line: str) -> Optional[str]:
    """若该行是变量定义，返回 KEY，否则返回 None。"""
    m = VAR_LINE.match(line.strip())
    return m.group(1) if m else None


def sync_env(
    example_path: Path,
    env_path: Path,
    dry_run: bool = False,
) -> bool:
    """
    将 example 中的变量同步到 .env，保留 .env 已有值。
    返回是否有写入/变更。
    """
    if not example_path.exists():
        print(f"[ERROR] 找不到: {example_path}", file=sys.stderr)
        return False

    example_text = example_path.read_text(encoding="utf-8")
    env_values = parse_env_content(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}

    # 从 .env.example 中解析出所有变量名及其“默认值”（用于新变量）
    example_vars: Dict[str, str] = {}
    for line in example_text.splitlines():
        key = extract_key_from_example_line(line)
        if key:
            # 解析该行的值作为默认值
            m = VAR_LINE.match(line.strip())
            if m:
                raw = m.group(2).strip()
                if " #" in raw:
                    raw = raw.split("#", 1)[0].strip()
                example_vars[key] = _unquote_value(raw)

    # 决定每个 key 的最终值：优先用 .env 已有值
    final_values: Dict[str, str] = {}
    for key, default in example_vars.items():
        final_values[key] = env_values.get(key, default)
    # 仅在 .env 中存在的 key（不在 example 中）保留并追加到末尾
    extra_in_env = {k: v for k, v in env_values.items() if k not in example_vars}

    # 按 .env.example 的顺序和结构生成新内容，遇到变量行则用 final_values 替换
    output_lines: list[str] = []
    for line in example_text.splitlines():
        key = extract_key_from_example_line(line)
        if key is not None and key in final_values:
            value = final_values[key]
            # 使用标准格式：KEY=value（等号两边不能有空格，符合 shell 规范）
            output_lines.append(f"{key}={_format_value(value)}")
        else:
            output_lines.append(line)

    if extra_in_env:
        output_lines.append("")
        output_lines.append("# ---------- 仅存在于 .env 的变量（未在 .env.example 中定义） ----------")
        for k, v in sorted(extra_in_env.items()):
            output_lines.append(f"{k}={_format_value(v)}")

    new_content = "\n".join(output_lines) + "\n"

    if dry_run:
        print("[DRY RUN] 将写入以下内容到 .env：")
        print(new_content[:2000] + ("..." if len(new_content) > 2000 else ""))
        return True

    if env_path.exists() and env_path.read_text(encoding="utf-8") == new_content:
        print("[OK] .env 已是最新，无需修改。")
        return True

    env_path.write_text(new_content, encoding="utf-8")
    print(f"[OK] 已更新 {env_path}（保留 .env 已有变量值，新增/同步 .env.example 中的变量）。")
    return True


def main():
    project_root = Path(__file__).resolve().parent.parent
    example_path = project_root / ".env.example"
    env_path = project_root / ".env"

    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    ok = sync_env(example_path, env_path, dry_run=dry_run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
