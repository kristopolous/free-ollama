#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.request

from dyva.imagegen import imagegen_cli


def _base_url(args):
    return f"http://{args.host}:{args.port}"


def _list_models(args):
    url = f"{_base_url(args)}/sdapi/v1/sd-models"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    for m in data.get("data", []):
        hosts = m.get("hosts", [])
        print(f"{m['id']}")
        if hosts:
            print(f"    hosts: {', '.join(hosts)}")
        else:
            print()


def main():
    parser = argparse.ArgumentParser(description="Generate images via dyva proxy (A1111 + ComfyUI)")
    parser.add_argument("--host", default="127.0.0.1", help="dyva proxy host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=11434, help="dyva proxy port (default: 11434)")
    parser.add_argument("--width", type=int, default=512, help="image width (default: 512)")
    parser.add_argument("--height", type=int, default=512, help="image height (default: 512)")
    parser.add_argument("--steps", type=int, default=30, help="sampling steps (default: 30)")
    parser.add_argument("-m", "--model", nargs="?", const="__list__", default=None, help="SD model name (use without value to list available models)")
    parser.add_argument("prompt", nargs="?", help="text prompt (default: read from stdin)")
    parser.add_argument("-o", "--output", help="output file (default: stdout as base64)")
    args = parser.parse_args()

    if args.model == "__list__":
        _list_models(args)
        return

    prompt = args.prompt
    if prompt is None:
        prompt = sys.stdin.read().strip()
    if not prompt:
        parser.print_help()
        sys.exit(1)

    try:
        img_bytes = imagegen_cli(
            prompt,
            model=args.model,
            width=args.width,
            height=args.height,
            steps=args.steps,
            host=args.host,
            port=args.port,
        )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        with open(args.output, "wb") as f:
            f.write(img_bytes)
    else:
        import base64
        sys.stdout.buffer.write(base64.b64encode(img_bytes))

if __name__ == "__main__":
    main()
