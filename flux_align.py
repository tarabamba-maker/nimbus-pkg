"""
flux_align.py — register the FLUX relight to the original with AI optical flow (RAFT).

Classic optical flow corrupts under big lighting changes; RAFT (a trained net) matches
STRUCTURE, so a relit image still aligns. Two safeguards keep it from ever distorting:
  • magnitude clamp  — no pixel moves more than `max_px`
  • flow smoothing   — Gaussian on the flow field removes stray vectors (the "tearing")

align(original, relit) -> a copy of `relit` warped onto the original's geometry.
"""
import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F
from torchvision.models.optical_flow import raft_large, Raft_Large_Weights

_MODEL = None
_DEV = "cpu"      # RAFT has ops flaky on MPS; CPU is reliable and fast enough per image


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = raft_large(weights=Raft_Large_Weights.DEFAULT, progress=False).to(_DEV).eval()
    return _MODEL


def _tensor(pil, h, w):
    img = pil.convert("RGB").resize((w, h), Image.BILINEAR)
    a = np.asarray(img, dtype=np.float32) / 255.0
    t = torch.from_numpy(a).permute(2, 0, 1).unsqueeze(0)
    return ((t - 0.5) / 0.5).to(_DEV)            # RAFT expects [-1, 1]


def _smooth_flow(flow, sigma):
    if sigma <= 0:
        return flow
    k = int(2 * round(3 * sigma) + 1)
    c = torch.arange(k, dtype=torch.float32) - (k - 1) / 2
    g = torch.exp(-(c ** 2) / (2 * sigma * sigma)); g /= g.sum()
    g = g.to(flow.device)
    kx, ky = g.view(1, 1, 1, k), g.view(1, 1, k, 1)
    pad = k // 2
    outs = []
    for ch in range(2):
        x = flow[:, ch:ch + 1]
        x = F.conv2d(F.pad(x, (pad, pad, 0, 0), mode="replicate"), kx)
        x = F.conv2d(F.pad(x, (0, 0, pad, pad), mode="replicate"), ky)
        outs.append(x)
    return torch.cat(outs, 1)


def align(original_pil, relit_pil, max_px=6.0, smooth=2.0, flow_long=1024):
    """Warp relit_pil onto original_pil's geometry. Both must be the same size."""
    W, H = original_pil.size
    s = flow_long / max(W, H)
    fw = max(8, int(round(W * s / 8)) * 8)       # flow res, divisible by 8
    fh = max(8, int(round(H * s / 8)) * 8)

    with torch.no_grad():
        o = _tensor(original_pil, fh, fw)
        r = _tensor(relit_pil, fh, fw)
        flow = _model()(o, r)[-1]                # original → relit, at flow res

        flow = F.interpolate(flow, size=(H, W), mode="bilinear", align_corners=False)
        flow[:, 0] *= W / fw                      # scale vectors to full res
        flow[:, 1] *= H / fh
        flow = _smooth_flow(flow, smooth)

        mag = torch.sqrt(flow[:, 0] ** 2 + flow[:, 1] ** 2) + 1e-6
        flow = flow * (torch.clamp(mag, max=max_px) / mag).unsqueeze(1)   # clamp move

        ys, xs = torch.meshgrid(torch.arange(H, dtype=torch.float32),
                                torch.arange(W, dtype=torch.float32), indexing="ij")
        gx = (xs + flow[0, 0]) / (W - 1) * 2 - 1
        gy = (ys + flow[0, 1]) / (H - 1) * 2 - 1
        grid = torch.stack((gx, gy), dim=-1).unsqueeze(0).to(_DEV)

        rel = torch.from_numpy(np.asarray(relit_pil.convert("RGB"), dtype=np.float32) / 255.0)
        rel = rel.permute(2, 0, 1).unsqueeze(0).to(_DEV)
        warped = F.grid_sample(rel, grid, mode="bilinear",
                               padding_mode="border", align_corners=True)

    arr = (warped[0].permute(1, 2, 0).clamp(0, 1).numpy() * 255).astype(np.uint8)
    return Image.fromarray(arr)
