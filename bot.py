#!/usr/bin/env python3
"""Zerito_Lol Discord automation bot.
Runs once per invocation (GitHub Actions cron). Stdlib + Pillow only.
Modules: Twitch live/offline, YouTube videos/shorts, TikTok, Riot ranked Solo/Duo cards.
State is kept in state.json (committed by the workflow) so nothing is posted twice.
"""
import io, json, os, re, sys, time, uuid, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ZeritoBot/1.0"}
PINK, PURPLE = 0xFF2E9A, 0x8A2BE2


# ---------- http helpers ----------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(url, method="GET", headers=None, data=None, retries=3, timeout=20, follow=True):
    h = dict(UA); h.update(headers or {})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            op = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
            with op.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if not follow and e.code in (301, 302, 303, 307, 308):
                return e.code, b""
            if e.code == 429:
                time.sleep(min(int(e.headers.get("Retry-After", "5")), 30)); last = e; continue
            if 400 <= e.code < 500:
                return e.code, e.read()
            last = e
        except Exception as e:
            last = e
        time.sleep(2 ** i)
    raise RuntimeError(f"HTTP failed {url}: {last}")


def jget(url, headers=None):
    code, body = http(url, headers=headers)
    if code != 200:
        raise RuntimeError(f"{code} {url} {body[:200]!r}")
    return json.loads(body)


# ---------- discord ----------
def webhook(name, embed=None, content=None, image=None):
    """Post to webhook env var `name`. image=(filename, bytes) attaches a PNG."""
    url = os.environ.get(name, "").strip()
    if not url:
        print(f"[skip] {name} not set"); return False
    payload = {"username": "Zerito_Lol", "allowed_mentions": {"parse": []}}
    if content: payload["content"] = content
    if embed: payload["embeds"] = [embed]
    url += ("&" if "?" in url else "?") + "wait=true"
    if image:
        b = "----z" + uuid.uuid4().hex
        fn, raw = image
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                 f'Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n'.encode(),
                 f'--{b}\r\nContent-Disposition: form-data; name="files[0]"; filename="{fn}"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode(), raw, f'\r\n--{b}--\r\n'.encode()]
        code, body = http(url, "POST", {"Content-Type": f"multipart/form-data; boundary={b}"}, b"".join(parts))
    else:
        code, body = http(url, "POST", {"Content-Type": "application/json"}, json.dumps(payload).encode())
    if code >= 300:
        raise RuntimeError(f"webhook {name} -> {code} {body[:200]!r}")
    return True


def log_error(state, key, msg):
    """Report an error to the private logs channel once per distinct problem."""
    errs = state.setdefault("errors", {})
    if errs.get(key) == msg[:200]:
        return
    errs[key] = msg[:200]
    print(f"[error] {key}: {msg}", file=sys.stderr)
    try:
        webhook("WEBHOOK_LOGS", content=f"⚠️ **{key}**: {msg[:1500]}")
    except Exception as e:
        print("log webhook failed", e, file=sys.stderr)


def clear_error(state, key):
    state.setdefault("errors", {}).pop(key, None)


def brand(embed, color=PINK):
    embed["color"] = color
    embed["footer"] = {"text": "Zerito_Lol · Kingdom of Madness"}
    return embed



# ---------- shared drawing helpers ----------
def _font(sz, bold=False):
    from PIL import ImageFont
    for p in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else []) + \
             ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"]:
        try: return ImageFont.truetype(p, sz)
        except Exception: pass
    return ImageFont.load_default()


def _wrap(d, text, font, maxw, maxlines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= maxw: cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    if len(lines) > maxlines:
        lines = lines[:maxlines]; lines[-1] = lines[-1].rstrip(" .,") + "…"
    return lines


# ---------- Twitch ----------
_TW = {}


def twitch_headers():
    """App access token (client credentials), cached per run. Shared by live alerts and clips."""
    if "hd" in _TW: return _TW["hd"]
    cid, sec = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    code, body = http("https://id.twitch.tv/oauth2/token", "POST",
                      {"Content-Type": "application/x-www-form-urlencoded"},
                      urllib.parse.urlencode({"client_id": cid, "client_secret": sec,
                                              "grant_type": "client_credentials"}).encode())
    if code != 200:
        raise RuntimeError(f"twitch token {code}")
    _TW["hd"] = {"Client-Id": cid, "Authorization": f"Bearer {json.loads(body)['access_token']}"}
    return _TW["hd"]


def twitch(state):
    cid, sec = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not (cid and sec):
        print("[skip] twitch secrets"); return
    login = CFG["twitch_login"]
    hd = twitch_headers()
    data = jget("https://api.twitch.tv/helix/streams?user_login=" + login, hd)["data"]
    st = state.setdefault("twitch", {"live": False, "stream_id": None, "off": 0})
    link = f"https://twitch.tv/{login}"
    if data:
        s = data[0]; st["off"] = 0
        if st["stream_id"] != s["id"]:
            thumb = s["thumbnail_url"].replace("{width}", "1280").replace("{height}", "720") + f"?t={int(time.time())}"
            webhook("WEBHOOK_TWITCH", content="@everyone" if CFG.get("ping_everyone") else None,
                    embed=brand({"title": f"🔴 {s['user_name']} está en directo",
                                 "description": f"**{s['title']}**",
                                 "url": link,
                                 "fields": [{"name": "Juego", "value": s.get("game_name") or "—", "inline": True},
                                            {"name": "Ver ahora", "value": f"[twitch.tv/{login}]({link})", "inline": True}],
                                 "image": {"url": thumb}}))
            st.update(live=True, stream_id=s["id"], started=s["started_at"], game=s.get("game_name"))
    elif st["live"]:
        st["off"] += 1
        if st["off"] >= 2:  # avoid false "offline" on short drops
            dur = ""
            try:
                a = datetime.fromisoformat(st["started"].replace("Z", "+00:00"))
                m = int((datetime.now(timezone.utc) - a).total_seconds() // 60)
                dur = f"Duración aproximada: {m // 60} h {m % 60} min"
            except Exception:
                pass
            webhook("WEBHOOK_TWITCH", embed=brand({"title": "⚫ El directo ha terminado",
                    "description": f"Gracias por venir. {dur}\nMañana, sobre las 18:00 (hora española), más. [Canal]({link})"}, PURPLE))
            st.update(live=False, stream_id=None, off=0)



def twitch_clips(state):
    """Publish every new public clip of the channel (clips are polled; Twitch has no webhook for clips)."""
    if not os.environ.get("WEBHOOK_CLIPS"):
        print("[skip] WEBHOOK_CLIPS not set"); return
    if not (os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")):
        print("[skip] twitch secrets"); return
    hd = twitch_headers(); login = CFG["twitch_login"]
    st = state.setdefault("clips", {"seen": None})
    if not st.get("uid"):
        st["uid"] = jget("https://api.twitch.tv/helix/users?login=" + login, hd)["data"][0]["id"]
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = jget(f"https://api.twitch.tv/helix/clips?broadcaster_id={st['uid']}&first=50&started_at={since}", hd)["data"]
    if st["seen"] is None:  # first run: seed silently, never flood the channel
        st["seen"] = [c["id"] for c in data]; return
    games = st.setdefault("games", {})
    for c in sorted(data, key=lambda c: c["created_at"]):
        if c["id"] in st["seen"]:
            continue
        gid = c.get("game_id")
        if gid and gid not in games:
            try: games[gid] = jget("https://api.twitch.tv/helix/games?id=" + gid, hd)["data"][0]["name"]
            except Exception: games[gid] = ""
        fields = [{"name": "Creado por", "value": c.get("creator_name") or "—", "inline": True},
                  {"name": "Duración", "value": f"{int(round(c.get('duration', 0)))} s", "inline": True}]
        if games.get(gid): fields.insert(1, {"name": "Juego", "value": games[gid], "inline": True})
        webhook("WEBHOOK_CLIPS", embed=brand({"title": "🎬 " + (c.get("title") or "Nuevo clip"), "url": c["url"],
                "description": f"Nuevo clip de **{c.get('broadcaster_name', login)}** en Twitch.\n[▶ Ver el clip]({c['url']})",
                "fields": fields, "image": {"url": c["thumbnail_url"]},
                "author": {"name": "Zerito_Lol · Clips de Twitch"}}))
        st["seen"].append(c["id"])
    st["seen"] = st["seen"][-300:]


# ---------- YouTube ----------
def yt_channel_id(state):
    if CFG.get("yt_channel_id"):
        return CFG["yt_channel_id"]
    if state.get("yt_channel_id"):
        return state["yt_channel_id"]
    code, body = http("https://www.youtube.com/" + CFG["yt_handle"], headers={"Accept-Language": "es"})
    m = re.search(rb'"channelId":"(UC[\w-]{22})"', body) or re.search(rb'channel_id=(UC[\w-]{22})', body)
    if not m:
        raise RuntimeError("could not resolve YouTube channel id; set yt_channel_id in config.json")
    state["yt_channel_id"] = m.group(1).decode()
    return state["yt_channel_id"]


def is_short(vid):
    code, _ = http(f"https://www.youtube.com/shorts/{vid}", follow=False, retries=2)
    return code == 200


def parse_yt_feed(xml_bytes):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(xml_bytes).findall("a:entry", ns):
        out.append({"id": e.find("yt:videoId", ns).text, "title": e.find("a:title", ns).text,
                    "published": e.find("a:published", ns).text})
    return out


def youtube(state):
    cid = yt_channel_id(state)
    code, body = http(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    if code != 200:
        raise RuntimeError(f"youtube rss {code}")
    st = state.setdefault("youtube", {"seen": None})
    entries = parse_yt_feed(body)
    if st["seen"] is None:  # first run: seed silently, never flood the channel
        st["seen"] = [e["id"] for e in entries]; return
    for e in reversed(entries):
        if e["id"] in st["seen"]:
            continue
        short = is_short(e["id"])
        url = f"https://www.youtube.com/{'shorts/' if short else 'watch?v='}{e['id']}"
        embed = brand({"title": e["title"], "url": url,
                       "description": "Nuevo Short en el canal" if short else "Nuevo vídeo en el canal",
                       "image": {"url": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg"},
                       "author": {"name": "Zerito_Lol · YouTube"}}, PINK if short else PURPLE)
        webhook("WEBHOOK_YT_SHORTS" if short else "WEBHOOK_YT_VIDEOS", embed=embed)
        st["seen"].append(e["id"])
    st["seen"] = st["seen"][-200:]


# ---------- TikTok (no free official feed: several public sources, first that works wins) ----------
def tt_tikwm(user):
    d = jget("https://www.tikwm.com/api/user/posts?unique_id=" + urllib.parse.quote(user) + "&count=10")
    if d.get("code") != 0:
        raise RuntimeError(f"tikwm: {d.get('msg')}")
    return [{"id": str(v["video_id"]), "desc": v.get("title", ""), "cover": v.get("cover"),
             "ts": v.get("create_time", 0)} for v in d["data"]["videos"]]


def tt_profile(user):
    code, body = http(f"https://www.tiktok.com/@{user}", headers={"Accept-Language": "es-ES,es"})
    m = re.search(rb'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', body, re.S)
    if code != 200 or not m:
        raise RuntimeError(f"tiktok profile page {code}")
    items = []
    def walk(o):
        if isinstance(o, dict):
            if "id" in o and "desc" in o and ("video" in o or "createTime" in o):
                items.append({"id": str(o["id"]), "desc": o["desc"],
                              "cover": (o.get("video") or {}).get("cover"), "ts": int(o.get("createTime", 0))})
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(json.loads(m.group(1)))
    if not items:
        raise RuntimeError("tiktok profile: no items")
    return items


def tt_rsshub(user):
    last = None
    for host in CFG.get("rsshub_instances", ["https://rsshub.app"]):
        try:
            code, body = http(f"{host}/tiktok/user/@{user}", retries=1)
            if code != 200: raise RuntimeError(code)
            out = []
            for it in ET.fromstring(body).iter("item"):
                link = it.findtext("link", "")
                m = re.search(r"/video/(\d+)", link)
                if m:
                    out.append({"id": m.group(1), "desc": re.sub("<[^>]+>", "", it.findtext("title", "")),
                                "cover": None, "ts": 0})
            if out: return out
        except Exception as e:
            last = e
    raise RuntimeError(f"rsshub: {last}")


def _rss_items(body):
    out = []
    for it in ET.fromstring(body).iter("item"):
        link = it.findtext("link", "")
        m = re.search(r"/video/(\d+)", link)
        if m:
            desc = re.sub("<[^>]+>", "", it.findtext("title", "") or "")
            out.append({"id": m.group(1), "desc": desc, "cover": None, "ts": 0})
    return out


def tt_proxitok(user):
    last = []
    for host in CFG.get("proxitok_instances", []):
        try:
            code, body = http(f"{host}/@{user}/rss", retries=1, timeout=12)
            if code != 200: raise RuntimeError(code)
            out = _rss_items(body)
            if out: return out
            raise RuntimeError("no items")
        except Exception as e:
            last.append(f"{host}:{str(e)[:40]}")
    raise RuntimeError("proxitok " + ";".join(last))


def tt_embed(user):
    code, body = http(f"https://www.tiktok.com/embed/@{user}", headers={"Accept-Language": "es-ES,es"})
    ids = list(dict.fromkeys(re.findall(rb'/video/(\d{15,25})', body)))
    if code != 200 or not ids:
        raise RuntimeError(f"embed {code} ids={len(ids)}")
    return [{"id": i.decode(), "desc": "", "cover": None, "ts": 0} for i in ids]



def tt_meta(url):
    """Official public oEmbed: title, author and thumbnail of a TikTok (works without login)."""
    try:
        code, body = http("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe=""), retries=1, timeout=12)
        if code == 200: return json.loads(body)
    except Exception as e:
        print("oembed failed:", str(e)[:100])
    return {}


def tt_card(title, author, thumb):
    """Branded 'Holo Glass' card: thumbnail + title + creator. Returns PNG bytes."""
    from PIL import Image, ImageDraw
    W, H = 1000, 520
    img = Image.new("RGB", (W, H), (12, 8, 22)); d = ImageDraw.Draw(img)
    for x in range(W):
        t = x / W; d.line([(x, 0), (x, H)], fill=(int(18 + 52 * t), int(8 + 6 * t), int(40 + 46 * t)))
    d.rectangle([0, 0, W - 1, H - 1], outline=(255, 46, 154), width=3)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
    od.rounded_rectangle([300, 28, W - 28, H - 28], 26, fill=(255, 255, 255, 22), outline=(255, 120, 200, 120), width=2)
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"); d = ImageDraw.Draw(img)
    ph = Image.new("RGB", (240, 426), (40, 30, 70))
    if thumb:
        try:
            im = Image.open(io.BytesIO(thumb)).convert("RGB"); r = max(240 / im.width, 426 / im.height)
            im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1)); x0, y0 = (im.width - 240) // 2, (im.height - 426) // 2
            ph = im.crop((x0, y0, x0 + 240, y0 + 426))
        except Exception: pass
    m = Image.new("L", (240, 426), 0); ImageDraw.Draw(m).rounded_rectangle([0, 0, 239, 425], 22, fill=255)
    d.rounded_rectangle([34, 40, 286, 482], 28, fill=(255, 46, 154)); img.paste(ph, (40, 47), m)
    d.text((330, 52), "NUEVO TIKTOK", font=_font(26, True), fill=(255, 120, 200))
    d.text((W - 250, 52), "ZERITO_LOL", font=_font(26, True), fill=(255, 255, 255))
    f = _font(40, True)
    y = 118
    for ln in _wrap(d, title or "Nuevo vídeo en TikTok", f, W - 330 - 60, 5):
        d.text((330, y), ln, font=f, fill=(255, 255, 255)); y += 54
    d.text((330, H - 120), f"@{author}", font=_font(30, True), fill=(220, 190, 255))
    d.rounded_rectangle([330, H - 76, 600, H - 40], 18, fill=(255, 46, 154))
    d.text((352, H - 71), "▶  Ver en TikTok", font=_font(22, True), fill=(255, 255, 255))
    out = io.BytesIO(); img.save(out, "PNG"); return out.getvalue()


def tiktok(state):
    user = CFG["tiktok_user"]
    errs, items = [], None
    for fn in (tt_tikwm, tt_embed, tt_profile, tt_proxitok, tt_rsshub):
        try:
            items = fn(user); break
        except Exception as e:
            errs.append(f"{fn.__name__}: {e}")
    if items is None:
        raise RuntimeError("all TikTok sources failed: " + " | ".join(e[:160] for e in errs))
    st = state.setdefault("tiktok", {"seen": None})
    if st["seen"] is None:
        st["seen"] = [i["id"] for i in items]; return
    for it in sorted(items, key=lambda i: i["ts"] or int(i["id"])):
        if it["id"] in st["seen"]:
            continue
        url = f"https://www.tiktok.com/@{user}/video/{it['id']}"
        meta = tt_meta(url)
        title = (meta.get("title") or it["desc"] or "").strip()
        cover = meta.get("thumbnail_url") or it.get("cover")
        emb = {"title": (title[:240] or "Nuevo TikTok"), "url": url,
               "description": f"Nuevo TikTok de **@{user}**\n[▶ Ver en TikTok]({url})",
               "author": {"name": f"@{meta.get('author_unique_id') or user} · TikTok"}}
        sent = False
        try:  # rich branded card; any failure falls back to the plain embed so the notification never breaks
            thumb = http(cover, retries=1, timeout=15)[1] if cover else None
            png = tt_card(title, user, thumb)
            emb["image"] = {"url": "attachment://tiktok.png"}
            webhook("WEBHOOK_TIKTOK", embed=brand(emb), image=("tiktok.png", png)); sent = True
        except Exception as e:
            print("tiktok card failed, plain embed:", str(e)[:120])
        if not sent:
            emb.pop("image", None)
            if cover: emb["image"] = {"url": cover}
            webhook("WEBHOOK_TIKTOK", embed=brand(emb))
        st["seen"].append(it["id"])
    st["seen"] = st["seen"][-200:]


# ---------- Riot ranked cards ----------
TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIVS = {"IV": 0, "III": 1, "II": 2, "I": 3}
TIER_ES = {"IRON": "Hierro", "BRONZE": "Bronce", "SILVER": "Plata", "GOLD": "Oro", "PLATINUM": "Platino",
           "EMERALD": "Esmeralda", "DIAMOND": "Diamante", "MASTER": "Maestro", "GRANDMASTER": "Gran Maestro",
           "CHALLENGER": "Aspirante"}


def lp_value(tier, div, lp):
    t = TIERS.index(tier)
    return 2800 + lp if t >= 7 else t * 400 + DIVS.get(div, 0) * 100 + lp


def riot_get(path, host):
    return jget(f"https://{host}.api.riotgames.com{path}", {"X-Riot-Token": os.environ["RIOT_API_KEY"]})


QUEUES = {420: "Clasificatoria Solo/Dúo", 440: "Clasificatoria Flexible", 450: "ARAM", 400: "Normal Reclutamiento",
          430: "Normal a ciegas", 490: "Partida rápida", 700: "Clash", 480: "Normal Swiftplay"}


def render_card(m, rank_txt, lp_txt, icon_bytes=None):
    """Branded result card. m: win, remake, champ, queue, k, d, a, cs, dur, when."""
    from PIL import Image, ImageDraw
    W, H = 900, 260
    win, remake = m["win"], m.get("remake")
    img = Image.new("RGB", (W, H), (14, 10, 24)); d = ImageDraw.Draw(img)
    for x in range(W):
        t = x / W
        base = (int(30 + 60 * t), int(12 + 8 * t), int(52 + 40 * t)) if win and not remake else (int(24 + 20 * t), int(16 + 6 * t), int(40 + 22 * t))
        d.line([(x, 0), (x, H)], fill=base)
    accent = (255, 46, 154) if win and not remake else (140, 120, 190)
    d.rectangle([0, 0, 10, H], fill=accent)
    ic = Image.new("RGB", (150, 150), (40, 30, 70))
    if icon_bytes:
        try: ic = Image.open(io.BytesIO(icon_bytes)).convert("RGB").resize((150, 150))
        except Exception: pass
    mask = Image.new("L", (150, 150), 0); ImageDraw.Draw(mask).ellipse([0, 0, 149, 149], fill=255)
    ring = Image.new("RGB", (166, 166), accent); rm = Image.new("L", (166, 166), 0)
    ImageDraw.Draw(rm).ellipse([0, 0, 165, 165], fill=255)
    img.paste(ring, (34, 46), rm); img.paste(ic, (42, 54), mask)
    label = "REMAKE" if remake else ("VICTORIA" if win else "DERROTA")
    d.text((34, 14), label, font=_font(22, True), fill=accent)
    d.text((230, 22), m["champ"], font=_font(34, True), fill=(255, 255, 255))
    d.text((230, 70), f"{m['k']} / {m['d']} / {m['a']}", font=_font(52, True), fill=(255, 255, 255))
    kda = (m["k"] + m["a"]) / max(1, m["d"])
    d.text((230, 138), f"{kda:.2f} KDA   ·   {m['cs']} CS ({m['cs'] / max(1, m['dur'] / 60):.1f}/min)   ·   {m['dur'] // 60}:{m['dur'] % 60:02d}",
           font=_font(22), fill=(220, 210, 240))
    d.text((230, 186), rank_txt or m["queue"], font=_font(26, True), fill=(255, 150, 210))
    if lp_txt:
        d.text((230, 222), lp_txt, font=_font(22, True), fill=(255, 255, 255) if win else (200, 190, 230))
    d.text((W - 250, H - 34), m["when"], font=_font(18), fill=(170, 160, 200))
    d.text((W - 250, 16), "ZERITO_LOL", font=_font(22, True), fill=(255, 120, 200))
    out = io.BytesIO(); img.save(out, "PNG"); return out.getvalue()


def riot(state):
    """Every finished LoL game (ranked, flex, ARAM, normals) -> branded card in #zerito-stats (WEBHOOK_STATS);
    Solo/Duo games additionally go to the ranked channel (WEBHOOK_RANKED) with LP change. No duplicates."""
    if not os.environ.get("RIOT_API_KEY"):
        print("[skip] RIOT_API_KEY"); return
    plat, reg = CFG.get("riot_platform", "euw1"), CFG.get("riot_region", "europe")
    st = state.setdefault("riot", {"seen": None})
    if not st.get("puuid"):
        acc = riot_get(f"/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(CFG['riot_name'])}/{urllib.parse.quote(CFG['riot_tag'])}", reg)
        st["puuid"] = acc["puuid"]
    puuid = st["puuid"]
    entries = riot_get(f"/lol/league/v4/entries/by-puuid/{puuid}", plat)
    solo = next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None)
    ids = riot_get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids?count=15", reg)  # all queues
    if st["seen"] is None:  # first run: just remember current state
        st["seen"] = ids; st["snap"] = snap_of(solo); return
    new = [i for i in reversed(ids) if i not in st["seen"]]
    if not new:
        st["snap"] = snap_of(solo) or st.get("snap"); return
    ver = None
    try: ver = jget("https://ddragon.leagueoflegends.com/api/versions.json")[0]
    except Exception: pass
    prev, cur = st.get("snap"), snap_of(solo)
    total = (cur["v"] - prev["v"]) if (cur and prev) else None
    ranked_new = []  # ids of Solo/Duo games among the new ones, in order
    for mid in new:
        info = riot_get(f"/lol/match/v5/matches/{mid}", reg)["info"]
        if info.get("queueId") == 420: ranked_new.append(mid)
        p = next((x for x in info["participants"] if x["puuid"] == puuid), None)
        qid = info.get("queueId")
        if p is None or qid == 1700 or qid not in QUEUES and info.get("gameMode") not in ("ARAM", "CLASSIC"):
            st["seen"].append(mid); continue  # Arena / unknown modes: no card (no honest win/loss for them)
        end = datetime.fromtimestamp(info["gameEndTimestamp"] / 1000, timezone.utc).astimezone()
        remake = bool(p.get("gameEndedInEarlySurrender")) or info["gameDuration"] < 300
        m = {"win": p["win"], "remake": remake, "champ": p["championName"], "k": p["kills"], "d": p["deaths"], "a": p["assists"],
             "cs": p["totalMinionsKilled"] + p.get("neutralMinionsKilled", 0), "dur": int(info["gameDuration"]),
             "queue": QUEUES.get(qid, "ARAM" if info.get("gameMode") == "ARAM" else "Partida"),
             "when": end.strftime("%d/%m/%Y %H:%M")}
        is_ranked = qid == 420
        lp_txt = rank_txt = ""
        if is_ranked and not remake and mid == new[-1] and total is not None:
            lp_txt = f"{'+' if total >= 0 else ''}{total} LP"
        if is_ranked and cur and mid == new[-1]:
            rank_txt = (f"{TIER_ES[cur['tier']]} {cur['lp']} LP" if TIERS.index(cur["tier"]) >= 7
                        else f"{TIER_ES[cur['tier']]} {cur['div']} · {cur['lp']} LP")
        icon = None
        if ver:
            try: icon = http(f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{p['championName']}.png")[1]
            except Exception: pass
        png = render_card(m, rank_txt, lp_txt, icon)
        name, tag = CFG["riot_name"], CFG["riot_tag"]
        opgg = f"https://www.op.gg/summoners/{plat[:-1] if plat[-1].isdigit() else plat}/{urllib.parse.quote(name)}-{tag}"
        ugg = f"https://u.gg/lol/profile/{plat}/{urllib.parse.quote(name.lower())}-{tag.lower()}/overview"
        title = ("Remake" if remake else "Victoria" if m["win"] else "Derrota") + f" con {m['champ']} · {m['queue']}"
        emb = brand({"title": title, "url": opgg,
                     "description": f"[Historial en OP.GG]({opgg}) · [U.GG]({ugg})",
                     "image": {"url": "attachment://partida.png"}}, PINK if m["win"] and not remake else PURPLE)
        emb["footer"]["text"] += f" · {mid}"
        sent = False
        if os.environ.get("WEBHOOK_STATS"):
            webhook("WEBHOOK_STATS", image=("partida.png", png), embed=emb); sent = True
        if is_ranked and os.environ.get("WEBHOOK_RANKED"):
            webhook("WEBHOOK_RANKED", image=("partida.png", png), embed=emb); sent = True
        if not sent: print("[skip] no stats webhook configured; game not marked as seen"); continue
        st["seen"].append(mid)
    st["seen"] = st["seen"][-150:]
    st["snap"] = cur or prev


def snap_of(e):
    if not e: return None
    return {"tier": e["tier"], "div": e.get("rank", "I"), "lp": e["leaguePoints"],
            "v": lp_value(e["tier"], e.get("rank", "I"), e["leaguePoints"])}


# ---------- main ----------
def load_secrets():
    """Single multi-line secret BOT_SECRETS (KEY=VALUE per line) -> environment."""
    for line in os.environ.get("BOT_SECRETS", "").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main():
    load_secrets()
    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
    only = set(sys.argv[1:])
    for name, fn in (("twitch", twitch), ("clips", twitch_clips), ("youtube", youtube), ("tiktok", tiktok), ("riot", riot)):
        if only and name not in only:
            continue
        try:
            fn(state); clear_error(state, name)
        except Exception as e:
            log_error(state, name, str(e))
    json.dump(state, open(STATE_PATH, "w"), indent=1)


if __name__ == "__main__":
    main()
