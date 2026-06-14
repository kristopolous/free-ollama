#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.request
import base64

def main():
    parser = argparse.ArgumentParser(description="Generate images via dyva proxy (A1111 txt2img)")
    parser.add_argument("--host", default="127.0.0.1", help="dyva proxy host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=11434, help="dyva proxy port (default: 11434)")
    parser.add_argument("--width", type=int, default=512, help="image width (default: 512)")
    parser.add_argument("--height", type=int, default=512, help="image height (default: 512)")
    parser.add_argument("--prompt", help="text prompt (default: read from stdin)")
    parser.add_argument("-o", "--output", help="output file (default: stdout as base64)")
    args = parser.parse_args()

    prompt = args.prompt
    if prompt is None:
        prompt = sys.stdin.read().strip()
    if not prompt:
        parser.print_help()
        sys.exit(1)

    url = f"http://{args.host}:{args.port}/sdapi/v1/txt2img"
    body = json.dumps({
        "prompt": prompt,
        "width": args.width,
        "height": args.height,
    }).encode()

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    images = data.get("images", [])
    if not images:
        print("error: no images in response", file=sys.stderr)
        sys.exit(1)

    raw = base64.b64decode(images[0])

    if args.output:
        with open(args.output, "wb") as f:
            f.write(raw)
    else:
        sys.stdout.buffer.write(raw)

if __name__ == "__main__":
    main()
