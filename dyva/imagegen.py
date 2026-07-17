"""
Image generation library for dyva.

Usage:
    from dyva.imagegen import imagegen

    img_bytes = await imagegen(prompt="a sunset over mountains")
    with open("out.png", "wb") as f:
        f.write(img_bytes)

Talks to a running dyva proxy at http://{host}:{port}/sdapi/v1/txt2img.
The proxy handles A1111 and ComfyUI host discovery automatically.
"""

import asyncio
import base64
import json
import urllib.request
import urllib.error


async def imagegen(
    prompt,
    *,
    model=None,
    width=512,
    height=512,
    steps=30,
    cfg_scale=7,
    sampler_name="Euler",
    negative_prompt="",
    seed=-1,
    host="127.0.0.1",
    port=11434,
    timeout=180,
):
    """Generate an image from a text prompt.

    Returns raw PNG bytes. Raises on error.
    """
    import aiohttp

    payload = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sampler_name": sampler_name,
        "negative_prompt": negative_prompt,
        "seed": seed,
    }
    if model:
        payload["model"] = model

    url = f"http://{host}:{port}/sdapi/v1/txt2img"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"imagegen failed: HTTP {resp.status}: {body}")
            data = await resp.json()

    images = data.get("images", [])
    if not images:
        raise RuntimeError("imagegen: no images in response")

    return base64.b64decode(images[0])


def imagegen_sync(prompt, **kwargs):
    """Synchronous wrapper around imagegen()."""
    return asyncio.run(imagegen(prompt, **kwargs))


def imagegen_cli(prompt, **kwargs):
    """Generate an image and return the payload dict (for CLI use)."""
    import aiohttp

    async def _inner():
        payload = {
            "prompt": prompt,
            "width": kwargs.get("width", 512),
            "height": kwargs.get("height", 512),
            "steps": kwargs.get("steps", 30),
            "cfg_scale": kwargs.get("cfg_scale", 7),
            "sampler_name": kwargs.get("sampler_name", "Euler"),
            "negative_prompt": kwargs.get("negative_prompt", ""),
            "seed": kwargs.get("seed", -1),
        }
        if kwargs.get("model"):
            payload["model"] = kwargs["model"]

        host = kwargs.get("host", "127.0.0.1")
        port = kwargs.get("port", 11434)
        timeout_s = kwargs.get("timeout", 180)
        url = f"http://{host}:{port}/sdapi/v1/txt2img"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"imagegen failed: HTTP {resp.status}: {body}")
                return await resp.json()

    return asyncio.run(_inner())
