#!/usr/bin/env python3
"""
flux_recolor.py — relight stock photos via BFL FLUX 2 [max].

Drop images into  flux_in/
Run            :  python3 flux_recolor.py
Get results in :  flux_in/output/<name>.psd

Each output PSD has TWO layers, exactly the same pixel size as the original:
    • bottom = your original photo (real pixels)
    • top    = FLUX relight   (use as a relight layer in Photoshop)

Cost control: FLUX is asked for ~1024 px on the long side (cheapest), then the
relight is upscaled back to the original's EXACT W×H so the two layers overlay
1:1. Lines may drift slightly — that's expected; the size match is guaranteed.

Prompt: edit flux_in/prompt.txt (falls back to DEFAULT_PROMPT below).
"""
import os, sys, json, time, base64, io, ssl
import urllib.request, urllib.error
from PIL import Image

# macOS system Python doesn't trust the Keychain root store (and a corporate proxy
# may inject a self-signed root), so default verification fails where curl works.
# Prefer certifi's CA bundle; fall back to an unverified context.
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()
import numpy as np
from pytoshop.user import nested_layers
from pytoshop.enums import ColorMode, Compression

BASE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.expanduser("~/Desktop/flux_in")           # drop photos here
OUT_DIR = os.path.join(IN_DIR, "output")                   # finished .psd sandwiches
CFG = os.path.join(BASE, "recipes", "_flux_config.json")

DEFAULT_PROMPT = (
    "Scene: Identical to input image. Preserve composition, objects, and textures "
    "pixel-for-pixel. Lighting: Direct, strong natural sunlight (approx. 2 PM clear "
    "day sun). Application: Apply globally. Lighting must be physically accurate. Add "
    "crisp directional shadows, soft diffused bounce light, and detailed specular "
    "reflections. Output: 8k photorealistic image where all original elements remain "
    "fixed, but are illuminated by new, realistic daylight."
)
EXTS = (".jpg", ".jpeg", ".png", ".webp")


def cfg():
    with open(CFG) as f:
        return json.load(f)


def prompt_text():
    p = os.path.join(IN_DIR, "prompt.txt")
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read().strip()
        if t:
            return t
    return DEFAULT_PROMPT


def target_size(w, h, long_side, step=64):
    """Pick FLUX request dims (multiples of `step`, both sides) whose aspect is the
    CLOSEST possible to the photo's, near the `long_side` budget — so the crop needed
    to match it is minimal (often zero)."""
    R = w / h
    L0 = max(step, round(long_side / step) * step)
    best = None
    for L in (L0 - step, L0, L0 + step):
        if L < step:
            continue
        if R >= 1:                                   # landscape: long side = width
            tw = L
            th = max(step, round(tw / R / step) * step)
        else:                                        # portrait: long side = height
            th = L
            tw = max(step, round(th * R / step) * step)
        err = abs((tw / th) - R)
        if best is None or err < best[0]:
            best = (err, tw, th)
    return best[1], best[2]


def crop_to_aspect(img, ratio):
    """Center-crop img to the exact aspect `ratio` (= w/h), keeping the largest area."""
    W, H = img.size
    if W / H > ratio:                      # too wide → trim width
        nw, nh = round(H * ratio), H
    else:                                  # too tall → trim height
        nw, nh = W, round(W / ratio)
    left, top = (W - nw) // 2, (H - nh) // 2
    return img.crop((left, top, left + nw, top + nh))


def post_json(url, payload, key):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-key": key, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:500]}")


def get_json(url, key):
    req = urllib.request.Request(url, headers={"x-key": key, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        return json.loads(r.read())


def flux_relight(pil_img, prompt, key, model, tw, th, extra=None):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {"prompt": prompt, "input_image": b64,
               "width": tw, "height": th,
               "output_format": "png", "safety_tolerance": 2}
    if extra:
        payload.update(extra)            # e.g. guidance, steps (flux-2-flex)
    submit = post_json(f"https://api.bfl.ai/v1/{model}", payload, key)
    poll = submit.get("polling_url") or f"https://api.bfl.ai/v1/get_result?id={submit['id']}"
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(2)
        res = get_json(poll, key)
        st = res.get("status")
        if st == "Ready":
            sample = res["result"]["sample"]
            with urllib.request.urlopen(sample, timeout=120, context=SSL_CTX) as r:
                return Image.open(io.BytesIO(r.read())).convert("RGB")
        if st and st not in ("Pending", "Queued", "Processing", "Request Accepted"):
            raise RuntimeError(f"FLUX status={st}: {res.get('result') or res}")
    raise TimeoutError("FLUX result timed out")


def layer(name, pil_img):
    arr = np.ascontiguousarray(np.asarray(pil_img, dtype=np.uint8))
    H, W = arr.shape[:2]
    chans = {i: np.ascontiguousarray(arr[:, :, i]) for i in range(3)}
    chans[-1] = np.full((H, W), 255, dtype=np.uint8)   # opaque alpha (pytoshop needs it)
    return nested_layers.Image(name=name, visible=True, opacity=255,
                               top=0, left=0, bottom=H, right=W,
                               channels=chans, color_mode=ColorMode.rgb)


def save_psd(out_path, original, relight):
    top = layer("FLUX relight", relight)       # index 0 = top-most
    bottom = layer("Original", original)
    psd = nested_layers.nested_layers_to_psd([top, bottom], color_mode=ColorMode.rgb,
                                             compression=Compression.raw)
    with open(out_path, "wb") as f:
        psd.write(f)


PS_APP = "Adobe Photoshop 2026"


def open_in_ps(psd_path, auto_align):
    """Open the PSD in Photoshop; optionally select both layers and Auto-Align them.
    Best-effort — any scripting failure just leaves the file open normally."""
    if not auto_align:
        os.system(f'open -a "{PS_APP}" "{psd_path}" 2>/dev/null || open "{psd_path}"')
        return
    jsx = f'''
try {{
    app.open(new File({json.dumps(psd_path)}));
    var doc = app.activeDocument;
    try {{ app.runMenuItem(stringIDToTypeID("selectAllLayers")); }} catch(e) {{}}
    var d = new ActionDescriptor();
    var r = new ActionReference();
    r.putEnumerated(charIDToTypeID("Lyr "), charIDToTypeID("Ordn"), charIDToTypeID("Trgt"));
    d.putReference(charIDToTypeID("null"), r);
    d.putEnumerated(charIDToTypeID("Usng"),
        stringIDToTypeID("alignDistributeSelector"), stringIDToTypeID("ADSAuto"));
    d.putEnumerated(stringIDToTypeID("projection"),
        stringIDToTypeID("projection"), stringIDToTypeID("auto"));
    executeAction(charIDToTypeID("Algn"), d, DialogModes.NO);
}} catch(e) {{}}
'''
    jsx_path = os.path.join(OUT_DIR, "_open_align.jsx")
    with open(jsx_path, "w") as f:
        f.write(jsx)
    rc = os.system(f'osascript -e \'tell application "{PS_APP}" to do javascript '
                   f'(read POSIX file "{jsx_path}")\' 2>/dev/null')
    if rc != 0:   # Photoshop not scriptable / not running → plain open
        os.system(f'open -a "{PS_APP}" "{psd_path}" 2>/dev/null || open "{psd_path}"')


def main():
    c = cfg()
    key = c["api_key"]
    model = c.get("model", "flux-2-max")
    long_side = int(c.get("long_side", 1024))
    extra = {}
    if "guidance" in c:
        extra["guidance"] = c["guidance"]
    if "steps" in c:
        extra["steps"] = c["steps"]
    prompt = prompt_text()
    os.makedirs(OUT_DIR, exist_ok=True)

    files = [f for f in sorted(os.listdir(IN_DIR))
             if f.lower().endswith(EXTS) and os.path.isfile(os.path.join(IN_DIR, f))]
    if not files:
        print(f"No images in {IN_DIR}. Drop photos there and rerun.")
        return

    print(f"Model: {model}  |  long side {long_side}px  |  {len(files)} image(s)\n")
    for fn in files:
        src = os.path.join(IN_DIR, fn)
        stem = os.path.splitext(fn)[0]
        out = os.path.join(OUT_DIR, stem + ".psd")
        if os.path.exists(out):
            print(f"• {fn}: already done, skipping")
            continue
        try:
            original = Image.open(src).convert("RGB")
            W, H = original.size
            tw, th = target_size(W, H, long_side)
            # Crop the ORIGINAL to FLUX's exact request aspect, pixel-perfect. Both the
            # cropped original and the relit result then share one aspect → the relight
            # upscales back with NO stretch and registers 1:1.
            cropped = crop_to_aspect(original, tw / th)
            cW, cH = cropped.size
            native_png = os.path.join(OUT_DIR, stem + "_flux_native.png")
            if os.path.exists(native_png):
                print(f"• {fn}: reusing cached FLUX native PNG")   # don't re-bill the API
                native = Image.open(native_png).convert("RGB")
            else:
                print(f"• {fn}  orig {W}×{H} → crop {cW}×{cH} → FLUX {tw}×{th} "
                      f"{extra or ''} …", flush=True)
                native = flux_relight(cropped, prompt, key, model, tw, th, extra)
                native.save(native_png)                            # cache RAW native first
            print(f"    FLUX returned native {native.size[0]}×{native.size[1]}")
            relit = native.resize((cW, cH), Image.LANCZOS)         # uniform upscale, no stretch
            if c.get("ai_align"):
                import flux_align
                print(f"    AI-align (RAFT, ±{c.get('align_max_px',6)}px) …", flush=True)
                relit = flux_align.align(cropped, relit,
                                         max_px=float(c.get("align_max_px", 6)),
                                         smooth=float(c.get("align_smooth", 2)))
            save_psd(out, cropped, relit)
            print(f"  ✅ {os.path.relpath(out, BASE)}  (2 layers, {cW}×{cH})")
            if c.get("open_in_photoshop", True):
                open_in_ps(out, c.get("auto_align", False))
        except Exception as e:
            print(f"  ❌ {fn}: {e}  (relight PNG is cached in output/ either way)")

    print("\nDone. Open the .psd files — bottom = your photo, top = relight layer.")


if __name__ == "__main__":
    main()
