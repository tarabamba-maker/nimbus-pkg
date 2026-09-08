"""
collectors/ms_plus.py — Microstock+ collectors.

Moved here from main.py (logic unchanged — only location changed):
  - _ms_plus_collect_direct
  - _ms_plus_collect_global
"""

import json
import os
import time
from io import BytesIO

import requests as req_lib
from PIL import Image, ImageOps

from sync_state import _sync_log, _sync_stop_flag
from cookies import _load_browser_cookies

_BASE_DIR = os.environ.get(
    "STOCK_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
RECIPES_DIR  = os.path.join(_BASE_DIR, "recipes")
MS_CACHE_DIR = os.path.join(_BASE_DIR, "img_cache_ms")
os.makedirs(MS_CACHE_DIR, exist_ok=True)


def _ms_plus_collect_direct():
    """MS+ via direct requests + in-app browser cookies. NO Playwright.
    Returns True on success (caller skips Playwright fallback).

    ⚠️ DO NOT TOUCH — works reliably. Tested: 163 folders + paginated photos in <1 min."""
    from image_utils import load_ms_library, save_ms_library

    _sync_log("🚀 Microstock+ direct: старт...")

    try:
        all_cookies = _load_browser_cookies()
    except Exception as e:
        _sync_log(f"⚠️ MS+ direct: cookies read failed: {e} — fallback")
        return False
    if not all_cookies:
        _sync_log("⚠️ MS+ direct: no browser cookies — log in via the app browser")
        return False

    ms_cookies = {c['name']: c['value'] for c in all_cookies
                  if 'microstock.plus' in c.get('domain', '')}
    if 'koa.sid' not in ms_cookies and 'session_debug' not in ms_cookies:
        _sync_log("⚠️ MS+ direct: no session cookie — fallback")
        return False

    base = "https://microstock.plus"
    session = req_lib.Session()
    session.cookies.update(ms_cookies)
    session.headers.update({
        "Accept": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.5 Safari/605.1.15"),
    })

    # Step 1: list directories
    try:
        r = session.post(f"{base}/contentdb/directories/getfulllist", data="", timeout=20)
        dirs_resp = r.json()
    except Exception as e:
        _sync_log(f"⚠️ MS+ direct: getfulllist exception: {e} — пропускаю цей синк")
        return "transient"   # network failure — not a login problem
    if not dirs_resp.get("isOk"):
        msg = dirs_resp.get("message", "unknown")
        if "сессия" in msg.lower() or "session" in msg.lower():
            _sync_log("⚠️ MS+ direct: session expired — потрібен логін")
            return False
        _sync_log(f"⚠️ MS+ direct: getfulllist failed: {msg} — пропускаю цей синк")
        return "transient"   # API-level failure, not auth

    dirs = [d for d in dirs_resp.get("list", []) if d.get("filestotal", 0) > 0]
    _sync_log(f"📁 MS+ direct: {len(dirs)} папок з файлами")

    # Skip-unchanged (v0.9.36): getfulllist gives filestotal per dir; if
    # unchanged since last successful sync, the dir's contents haven't been
    # touched on MS+ side (uploads/deletes change filestotal). Only fetch
    # contentlist for changed dirs.
    #
    # ⚠️ DO NOT remove _ms_dirs_state.json or weaken this check — without it
    # every sync re-fetches all 152 directories sequentially (~30-60s wasted).
    # To force a full re-sync (e.g., after suspected MS+ corruption):
    #   rm recipes/_ms_dirs_state.json
    _ms_state_file = os.path.join(RECIPES_DIR, "_ms_dirs_state.json")
    try:
        with open(_ms_state_file) as _f:
            _prev_filestotal = json.load(_f)
    except Exception:
        _prev_filestotal = {}
    _cur_filestotal = {d.get("path", ""): d.get("filestotal", 0) for d in dirs if d.get("path")}
    changed_dirs = [d for d in dirs
                    if _prev_filestotal.get(d.get("path", "")) != d.get("filestotal", 0)]
    if _prev_filestotal:
        _sync_log(f"⏩ MS+ direct: {len(changed_dirs)}/{len(dirs)} папок змінилось — фетч лише їх")
    else:
        _sync_log(f"🆕 MS+ direct: перший повний синк — {len(dirs)} папок")
        changed_dirs = dirs

    # Full agency list — even if we don't track sales for some, we want their
    # stockids for cross-matching coverage.
    AGENCIES = ["shutterstock", "esp", "adobestock", "dreamstime", "yaymicro",
                "123rf", "depositphotos", "alamy", "pixta", "pond5", "vecteezy",
                "photodune", "eyeem", "bigstockphoto", "freepik", "canva"]
    # ⚠️ MS+ API expects useragencyids as URL-encoded JSON array, NOT PHP-style
    # repeated key. The old "useragencyids[]=foo&useragencyids[]=bar" syntax
    # returns HTTP 500 from the server. Verified 2026-05-26.
    import urllib.parse as _up
    import json as _json
    agencies_param = "useragencyids=" + _up.quote(_json.dumps(AGENCIES))

    library = {}
    total_files = 0
    # Dirs whose contentlist pagination completed cleanly. ONLY these get their
    # new filestotal persisted below; a dir that errored/was stopped mid-way keeps
    # its OLD value so it is re-fetched next sync. (Previously every dir was
    # written with its new count regardless → partially fetched dirs were never
    # revisited until MS+'s count changed again.)
    ok_dirs = set()
    for dir_idx, dr in enumerate(changed_dirs, 1):
        if _sync_stop_flag[0]:
            _sync_log("⛔ MS+ direct: зупинено")
            break
        path = dr.get("path", "")
        if not path:
            continue
        path_enc = _up.quote(path, safe='')
        skip = 0
        dir_ok = True
        while True:
            body = (f"skip={skip}&limit=250&directory={path_enc}"
                    f"&{agencies_param}&searchtext=&archive=0")
            try:
                r = session.post(f"{base}/contentdb/content/contentlist",
                                data=body, timeout=20)
                resp = r.json()
            except Exception as ex:
                _sync_log(f"  ⚠️ {path} skip={skip}: {ex}")
                dir_ok = False
                break
            if not resp.get("isOk"):
                _sync_log(f"  ⚠️ {path} skip={skip}: {resp.get('message','?')}")
                dir_ok = False
                break
            if _sync_stop_flag[0]:
                dir_ok = False
                break
            items = resp.get("list", [])
            if not items:
                break
            for it in items:
                basepath = it.get("basepath", "")
                if not basepath:
                    continue
                # ⚠️ Key by basepath (unique across folders) — NOT just filename.
                # Camera reuses names like "B94A1234" every 10k shots, so multiple
                # different MS+ entries can share filename across different shoot
                # folders. Keying by filename loses ~40% of photos to silent dedup.
                filename = basepath.split("/")[-1]
                stockids = it.get("stockids") or {}
                stockids = {k: str(v) for k, v in stockids.items() if v}
                thumb_url = it.get("thumbnailurl", "") or it.get("previewurl", "")
                title = (it.get("metadata") or {}).get("basic", {}).get("title", "")
                # Group = LAST folder of MS+ directory path, drop year/month prefix.
                # "/2021/21-03 March/210110 Tanabash Office" → "210110 Tanabash Office"
                dir_path = (it.get("directory") or "").rstrip("/")
                group_name = dir_path.split("/")[-1] if dir_path else ""
                library[basepath] = {
                    "filename": filename,
                    "group":    group_name,
                    "stockids": stockids,
                    "basepath": basepath,
                    "thumb":    thumb_url,
                    "title":    title,
                }
                total_files += 1
            if len(items) < 250:
                break
            skip += 250
            time.sleep(0.1)
        if dir_ok:
            ok_dirs.add(path)
        if dir_idx % 20 == 0 or dir_idx == len(changed_dirs):
            _sync_log(f"  📦 {dir_idx}/{len(changed_dirs)} папок, всього {total_files} фото")

    # Merge with existing ms_library.json by basepath (unique key).
    # MS+ is master — if user moved a photo to a different folder in MS+,
    # the new group name wins. Local edits via UI are not preserved across
    # MS+ syncs (use manual overrides for permanent local-only group choices).
    existing = load_ms_library()
    existing_by_bp = {e.get("basepath", ""): e for e in existing if e.get("basepath")}
    moved = 0
    new_basepaths = set()
    for bp, rec in library.items():
        old = existing_by_bp.get(bp, {})
        if not old:
            new_basepaths.add(bp)
        elif old.get("group") and old["group"] != rec["group"]:
            moved += 1
        existing_by_bp[bp] = rec
    merged = list(existing_by_bp.values())
    if moved:
        _sync_log(f"📦 MS+ direct: {moved} фото перенесено в інші групи (MS+ master)")
    save_ms_library(merged)
    _sync_log(f"✅ MS+ direct: {total_files} записів, ms_library.json = {len(merged)} (unique basepaths)")

    # Step 4: download MS+ reference thumbnails to img_cache_ms/.
    # These serve as ground-truth pHash for matching sales photos against MS+
    # groups when sales asset_ids aren't in stockids (visual fallback path).
    # Iterate only NEW basepaths (added this sync) — old ones are already cached.
    if not new_basepaths:
        _sync_log("✅ MS+ thumbnails: нічого нового — skip")
        thumb_iter = []
    else:
        _sync_log(f"🖼️ MS+ direct: {len(new_basepaths)} нових мініатюр")
        thumb_iter = [r for r in merged if r.get("basepath") in new_basepaths]
    dl_count = skip_count = 0
    img_session = req_lib.Session()
    img_session.cookies.update(ms_cookies)
    img_session.headers.update({"User-Agent": session.headers["User-Agent"]})
    for rec in thumb_iter:
        if _sync_stop_flag[0]: break
        bp = rec.get("basepath", "")
        thumb = rec.get("thumb", "")
        if not bp or not thumb: continue
        # Disk filename derived from basepath (unique across same-filename camera shots)
        # Example: /2022/22-04-17 business coworkers/B94A9730-flipHorizontal
        # → 2022__22-04-17_business_coworkers__B94A9730-flipHorizontal.jpg
        safe_fn = bp.lstrip("/").replace("/", "__").replace("\\", "__")
        dest = os.path.join(MS_CACHE_DIR, f"{safe_fn}.jpg")
        if os.path.exists(dest):
            skip_count += 1; continue
        try:
            r = img_session.get(thumb, timeout=15)
            if r.status_code == 200 and r.content[:2] == b'\xff\xd8':
                # Normalize to 400x400 JPEG 88 — same form as load_img() saves
                # sales thumbs, so pHash computation is consistent.
                img = Image.open(BytesIO(r.content)).convert('RGB')
                img = ImageOps.fit(img, (400, 400), Image.Resampling.LANCZOS)
                img.save(dest, 'JPEG', quality=88)
                dl_count += 1
        except Exception:
            pass
        if (dl_count + skip_count) % 200 == 0:
            _sync_log(f"  📥 {dl_count} new, {skip_count} skipped (cached)")
    _sync_log(f"✅ MS+ thumbnails: {dl_count} new downloaded, {skip_count} cached")

    # Compute pHash only when there were new thumbnails (api_compute_ms_hashes
    # already short-circuits cached rows, but iterating 15k files is wasteful).
    if dl_count > 0:
        try:
            from main import flask_app
            from routes.matching import api_compute_ms_hashes
            with flask_app.app_context():
                api_compute_ms_hashes()
            _sync_log("✅ MS+ ms_meta pHashes computed")
        except Exception as ex:
            _sync_log(f"⚠️ ms_meta compute failed: {ex}")

    # Persist filestotal-per-dir cursor for next incremental sync — but only
    # advance dirs that were fetched completely; incomplete ones keep their old
    # value (or stay absent) so they are re-fetched next time.
    new_state = {}
    for d_path, cur_total in _cur_filestotal.items():
        if d_path in ok_dirs:
            new_state[d_path] = cur_total
        elif d_path in _prev_filestotal:
            new_state[d_path] = _prev_filestotal[d_path]
    n_pending = len([d for d in changed_dirs if d.get("path") and d.get("path") not in ok_dirs])
    if n_pending:
        _sync_log(f"⏸ MS+ direct: {n_pending} папок не дочитано — повторяться наступного синку")
    try:
        tmp = _ms_state_file + ".tmp"
        with open(tmp, 'w') as _f:
            json.dump(new_state, _f)
        os.replace(tmp, _ms_state_file)
    except Exception:
        pass
    return True


def _ms_plus_collect_global(pw_page):
    """Microstock+ → оновлює ms_library.json (stockids для крос-стокового матчингу).
    Не зберігає продажі — це reference-source. Викликає 2 ендпоінти:
      POST /contentdb/directories/getfulllist  → список папок
      POST /contentdb/content/contentlist      → фото в кожній папці (paged по 250)
    Структура збереженого запису: {filename, group, stockids: {adobestock, shutterstock, ...}}.
    """
    from image_utils import load_ms_library, save_ms_library

    _sync_log("🚀 Microstock+: оновлюю бібліотеку stockids...")

    if "microstock.plus" not in pw_page.url:
        pw_page.goto("https://microstock.plus/myfiles",
                     wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)

    if any(x in pw_page.url.lower() for x in ["login", "signin", "auth"]):
        _sync_log("🔒 Microstock+: потрібна авторизація")
        return

    def _fetch_json(path, body=""):
        body_js = json.dumps(body)
        js = f"""async () => {{
            try {{
                const r = await fetch("{path}", {{
                    method: "POST",
                    credentials: "include",
                    headers: {{
                        "x-requested-with": "XMLHttpRequest",
                        "content-type": "application/x-www-form-urlencoded"
                    }},
                    body: {body_js}
                }});
                if (!r.ok) return {{error: r.status}};
                return await r.json();
            }} catch(e) {{ return {{error: e.toString()}}; }}
        }}"""
        try:
            return pw_page.evaluate(js)
        except Exception as ex:
            return {"error": str(ex)}

    # Step 1: get list of all directories with file counts
    dirs_resp = _fetch_json("/contentdb/directories/getfulllist", "")
    if "error" in dirs_resp or not dirs_resp.get("isOk"):
        _sync_log(f"⚠️ Microstock+: getfulllist failed: {dirs_resp}")
        return
    dirs = [d for d in dirs_resp.get("list", []) if d.get("filestotal", 0) > 0]
    _sync_log(f"📁 Microstock+: {len(dirs)} папок з файлами")

    # Step 2: for each directory, fetch contentlist with pagination.
    # ⚠️ MS+ API expects useragencyids as URL-encoded JSON array, NOT PHP-style
    # repeated key. Old "useragencyids[]=..." syntax returns HTTP 500.
    AGENCIES = ["shutterstock", "esp", "adobestock", "dreamstime", "yaymicro",
                "123rf", "depositphotos", "alamy", "pixta", "pond5", "vecteezy"]
    import urllib.parse as _up2
    import json as _json2
    agencies_param = "useragencyids=" + _up2.quote(_json2.dumps(AGENCIES))

    library = {}    # filename → record
    total_files = 0

    for dir_idx, dr in enumerate(dirs, 1):
        if _sync_stop_flag[0]:
            _sync_log("⛔ Microstock+: зупинено")
            break
        path = dr.get("path", "")
        if not path:
            continue
        # urlencode the path manually (no JS quoting issues)
        import urllib.parse as _up
        path_enc = _up.quote(path, safe='')
        skip = 0
        dir_count = 0
        while True:
            body = (f"skip={skip}&limit=250&directory={path_enc}"
                    f"&{agencies_param}&searchtext=&archive=0")
            resp = _fetch_json("/contentdb/content/contentlist", body)
            if "error" in resp or not resp.get("isOk"):
                _sync_log(f"  ⚠️ {path} skip={skip}: {resp}")
                break
            items = resp.get("list", [])
            if not items:
                break
            for it in items:
                basepath = it.get("basepath", "")
                if not basepath:
                    continue
                filename = basepath.split("/")[-1]   # last segment as canonical key
                stockids = it.get("stockids") or {}
                # Normalize: keep only non-empty values
                stockids = {k: str(v) for k, v in stockids.items() if v}
                if not stockids:
                    continue
                dir_path = (it.get("directory") or "").rstrip("/")
                library[filename] = {
                    "filename": filename,
                    "group":    dir_path.split("/")[-1] if dir_path else "",
                    "stockids": stockids,
                    "basepath": basepath,
                }
                dir_count += 1
            if len(items) < 250:
                break
            skip += 250
            time.sleep(0.2)
        total_files += dir_count
        if dir_idx % 10 == 0 or dir_idx == len(dirs):
            _sync_log(f"  📦 {dir_idx}/{len(dirs)} папок, всього {total_files} фото")

    # Step 3: merge with existing ms_library.json (preserve manual edits / non-MS+ entries)
    existing = load_ms_library()
    existing_by_fn = {e.get("filename", ""): e for e in existing if e.get("filename")}
    for fn, rec in library.items():
        old = existing_by_fn.get(fn, {})
        # Preserve manual group override if user set one different from MS+ directory
        if old.get("group") and old["group"] != rec["group"]:
            rec["_ms_plus_group"] = rec["group"]
            rec["group"] = old["group"]
        existing_by_fn[fn] = rec

    merged = list(existing_by_fn.values())
    save_ms_library(merged)
    _sync_log(f"✅ Microstock+: {total_files} записів, ms_library.json = {len(merged)} всього")
