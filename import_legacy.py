"""Read legacy Python settings as literals, without running the scripts."""
import argparse
import ast
import json
import os
from pathlib import Path


def read_auth(path, fields):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in fields:
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                raise ValueError(f"{target.id} 必须是旧脚本中的字符串常量") from None
            if not isinstance(value, str):
                raise ValueError(f"{target.id} 必须是旧脚本中的字符串常量")
            values[target.id] = value
    missing = fields.keys() - values.keys()
    if missing:
        raise ValueError(f"旧脚本缺少字段 {'、'.join(sorted(missing))}")
    return values


def main():
    parser = argparse.ArgumentParser(description="从旧 IPTV 脚本导入鉴权设置")
    parser.add_argument("legacy_dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("config.local.json"))
    args = parser.parse_args()
    try:
        template = Path(__file__).with_name("config.example.json")
        config = json.loads(template.read_text(encoding="utf-8"))
        for name, entry in config["providers"].items():
            source = args.legacy_dir / f"getiptv_{name}.py"
            try:
                entry["auth"] = read_auth(source, entry["auth"])
            except SyntaxError:
                parser.error(f"{source.name} 有语法错误")
            except ValueError as exc:
                parser.error(f"{source.name} {exc}")
        with open(args.output, "x", encoding="utf-8", opener=private_open) as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        parser.error("输出文件已存在，请选择新路径")
    except OSError as exc:
        parser.error(f"无法读取或写入文件（{type(exc).__name__}）")
    print("已导入鉴权设置。请核对 github_repository 后再启用发布。")


def private_open(path, flags):
    return os.open(path, flags, 0o600)


if __name__ == "__main__":
    main()
