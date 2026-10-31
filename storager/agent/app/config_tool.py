"""节点声明配置校验/展示（组件镜像内一次性执行）。

CLI 的 `kurrent config validate|show` 与 `kurrent join` 预检经 stdin 调用本工具：
`docker run --rm -i --entrypoint python storager-agent -m app.config_tool ...`。
与运行时共用 config_schema.py，不存在第二套字段白名单；本模块不加载运行态单例。

退出码：0 = 通过；2 = YAML/校验错误。
"""

from __future__ import annotations

import argparse
import sys

import yaml
from pydantic import ValidationError

from .config_schema import NodeConfiguration, effective_config


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        lines.append(f"{loc}: {err.get('msg', 'invalid value')}")
    return "\n".join(lines)


def run(cmd: str, raw: str) -> int:
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"invalid YAML: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("configuration root must be a YAML mapping", file=sys.stderr)
        return 2
    try:
        cfg = NodeConfiguration.model_validate(data)
    except ValidationError as exc:
        print(_format_errors(exc), file=sys.stderr)
        return 2
    if cmd == "show":
        sys.stdout.write(yaml.safe_dump(
            effective_config(cfg), sort_keys=False, allow_unicode=True))
    else:
        print(f"ok: {cfg.metadata.name} backend={cfg.spec.agent.backend} "
              f"controlPlane={cfg.spec.control_plane.url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.config_tool")
    parser.add_argument("cmd", choices=("validate", "show"))
    parser.add_argument("--file", "-f", default="-",
                        help="配置路径（默认 stdin）")
    args = parser.parse_args(argv)
    return run(args.cmd, _read(args.file))


if __name__ == "__main__":
    sys.exit(main())
