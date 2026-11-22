#!/usr/bin/env python3
"""Zerito_Lol Discord automation bot.
Runs once per invocation (GitHub Actions cron). Stdlib + Pillow only.
Modules: Twitch live/offline, YouTube videos/shorts, TikTok, Riot ranked Solo/Duo cards.
State is kept in state.json (committed by the workflow) so nothing is posted twice.
"""
import io, json, os, re, sys, time, uuid, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

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
    embed["footer"] = {"text": "Zerito_Lol · Casa de Shaco"}
    return embed


# ---------- Twitch ----------
def twitch(state):
    cid, sec = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not (cid and sec):
        print("[skip] twitch secrets"); return
    login = CFG["twitch_login"]
    code, body = http("https://id.twitch.tv/oauth2/token", "POST",
                      {"Content-Type": "application/x-www-form-urlencoded"},
                      urllib.parse.urlencode({"client_id": cid, "client_secret": sec,
                                              "grant_type": "client_credentials"}).encode())
    if code != 200:
        raise RuntimeError(f"twitch token {code}")
    tok = json.loads(body)["access_token"]
    hd = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
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


def tiktok(state):
    user = CFG["tiktok_user"]
    errs, items = [], None
    for fn in (tt_tikwm, tt_profile, tt_rsshub):
        try:
            items = fn(user); break
        except Exception as e:
            errs.append(f"{fn.__name__}: {e}")
    if items is None:
        raise RuntimeError("all TikTok sources failed: " + " | ".join(errs))
    st = state.setdefault("tiktok", {"seen": None})
    if st["seen"] is None:
        st["seen"] = [i["id"] for i in items]; return
    for it in sorted(items, key=lambda i: i["ts"] or int(i["id"])):
        if it["id"] in st["seen"]:
            continue
        url = f"https://www.tiktok.com/@{user}/video/{it['id']}"
        emb = {"title": "Nuevo TikTok", "url": url, "description": (it["desc"] or "")[:300],
               "author": {"name": f"@{user} · TikTok"}}
        if it.get("cover"): emb["image"] = {"url": it["cover"]}
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


def render_card(m, rank_txt, lp_txt, icon_bytes=None):
    from PIL import Image, ImageDraw, ImageFont
    W, H = 900, 260
    win = m["win"]
    img = Image.new("RGB", (W, H), (14, 10, 24))
    d = ImageDraw.Draw(img)
    for x in range(W):  # horizontal gradient
        t = x / W
        base = (int(30 + 60 * t), int(12 + 8 * t), int(52 + 40 * t)) if win else (int(24 + 20 * t), int(16 + 6 * t), int(40 + 22 * t))
        d.line([(x, 0), (x, H)], fill=base)
    accent = (255, 46, 154) if win else (140, 120, 190)
    d.rectangle([0, 0, 10, H], fill=accent)

    def font(sz, bold=False):
        for p in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else []) + \
                 ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"]:
            try: return ImageFont.truetype(p, sz)
            except Exception: pass
        return ImageFont.load_default()
    # champion icon
    ic = Image.new("RGB", (150, 150), (40, 30, 70))
    if icon_bytes:
        try: ic = Image.open(io.BytesIO(icon_bytes)).convert("RGB").resize((150, 150))
        except Exception: pass
    mask = Image.new("L", (150, 150), 0); ImageDraw.Draw(mask).ellipse([0, 0, 149, 149], fill=255)
    ring = Image.new("RGB", (166, 166), accent); rm = Image.new("L", (166, 166), 0)
    ImageDraw.Draw(rm).ellipse([0, 0, 165, 165], fill=255)
    img.paste(ring, (34, 46), rm); img.paste(ic, (42, 54), mask)
    d.text((34, 14), "VICTORIA" if win else "DERROTA", font=font(22, True), fill=accent)
    d.text((230, 22), m["champ"], font=font(34, True), fill=(255, 255, 255))
    d.text((230, 70), f"{m['k']} / {m['d']} / {m['a']}", font=font(52, True), fill=(255, 255, 255))
    kda = (m["k"] + m["a"]) / max(1, m["d"])
    d.text((230, 138), f"{kda:.2f} KDA   ·   {m['cs']} CS ({m['cs'] / max(1, m['dur'] / 60):.1f}/min)   ·   {m['dur'] // 60}:{m['dur'] % 60:02d}",
           font=font(22), fill=(220, 210, 240))
    d.text((230, 186), f"{rank_txt}", font=font(26, True), fill=(255, 150, 210))
    if lp_txt:
        d.text((230, 222), lp_txt, font=font(22, True), fill=(255, 255, 255) if win else (200, 190, 230))
    d.text((W - 250, H - 34), m["when"], font=font(18), fill=(170, 160, 200))
    d.text((W - 250, 16), "ZERITO_LOL", font=font(22, True), fill=(255, 120, 200))
    out = io.BytesIO(); img.save(out, "PNG"); return out.getvalue()


def riot(state):
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
    ids = riot_get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&count=10", reg)  # queue 420 = Ranked Solo/Duo
    if st["seen"] is None:  # first run: just remember current state
        st["seen"] = ids; st["snap"] = snap_of(solo); return
    new = [i for i in reversed(ids) if i not in st["seen"]]
    if not new:
        st["snap"] = snap_of(solo) or st.get("snap"); return
    ver = None
    try: ver = jget("https://ddragon.leagueoflegends.com/api/versions.json")[0]
    except Exception: pass
    prev = st.get("snap")
    cur = snap_of(solo)
    total = (cur["v"] - prev["v"]) if (cur and prev) else None
    for n, mid in enumerate(new):
        info = riot_get(f"/lol/match/v5/matches/{mid}", reg)["info"]
        if info.get("queueId") != 420:
            st["seen"].append(mid); continue
        p = next(x for x in info["participants"] if x["puuid"] == puuid)
        end = datetime.fromtimestamp(info["gameEndTimestamp"] / 1000, timezone.utc).astimezone()
        m = {"win": p["win"], "champ": p["championName"], "k": p["kills"], "d": p["deaths"], "a": p["assists"],
             "cs": p["totalMinionsKilled"] + p.get("neutralMinionsKilled", 0), "dur": int(info["gameDuration"]),
             "when": end.strftime("%d/%m/%Y %H:%M")}
        last = n == len(new) - 1
        lp_txt = ""
        if last and total is not None:
            lp_txt = f"{'+' if total >= 0 else ''}{total} LP"
        rank_txt = ""
        if cur:
            rank_txt = (f"{TIER_ES[cur['tier']]} {cur['lp']} LP" if TIERS.index(cur["tier"]) >= 7
                        else f"{TIER_ES[cur['tier']]} {cur['div']} · {cur['lp']} LP") if last else ""
        icon = None
        if ver:
            try: icon = http(f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{p['championName']}.png")[1]
            except Exception: pass
        png = render_card(m, rank_txt or "Clasificatoria Solo/Dúo", lp_txt, icon)
        opgg = f"https://www.op.gg/summoners/{plat[:-1] if plat[-1].isdigit() else plat}/{urllib.parse.quote(CFG['riot_name'])}-{CFG['riot_tag']}"
        webhook("WEBHOOK_RANKED", image=("partida.png", png),
                embed=brand({"title": f"{'Victoria' if m['win'] else 'Derrota'} con {m['champ']}",
                             "url": opgg, "description": f"[Ver perfil en OP.GG]({opgg})",
                             "image": {"url": "attachment://partida.png"}}, PINK if m["win"] else PURPLE))
        st["seen"].append(mid)
    st["seen"] = st["seen"][-100:]
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
    for name, fn in (("twitch", twitch), ("youtube", youtube), ("tiktok", tiktok), ("riot", riot)):
        if only and name not in only:
            continue
        try:
            fn(state); clear_error(state, name)
        except Exception as e:
            log_error(state, name, str(e))
    json.dump(state, open(STATE_PATH, "w"), indent=1)


if __name__ == "__main__":
    main()
