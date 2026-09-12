#!/usr/bin/env python3
"""Fetch everything the tracker needs and write data.js.

  - Quest list (wiki page HTML, parsed in the browser)
  - Required items per quest (wiki page "Required Quest Item Totals", parsed here)
  - Tradeability of each item (wiki categories)
  - Quest completion per player (WikiSync; only for players with the plugin)
  - Levels, XP, clue counts and boss kill counts per player (official hiscores; live)
  - Number of combat achievement tasks (wiki), so the stats tab can show a fraction
  - Skill requirements, item skills, prerequisite quests and rewards per quest (from each quest page's wikitext)
  - Collection log history: WikiSync lists the log in a fixed order, so when each item
    was gained is recorded here by diffing against the previous run (HISTORY file)

Usage: python3 fetch_data.py [--refresh]

Wiki reference data (quest list, items, requirements, rewards, CA count) changes
rarely and is cached for a day (WIKI_CACHE file); --refresh forces a new fetch.
Player data is fetched every run. data.hash is written alongside data.js so the
deploy can be skipped when nothing has changed.
Only stdlib is used.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

PLAYERS = ["gaudyk", "uncle sacks", "Moms klit", "knysliukas", "cluescrollas"]


WIKI_API = "https://oldschool.runescape.wiki/api.php"
SYNC_URL = "https://sync.runescape.wiki/runelite/player/{name}/STANDARD"
HISCORES_URL = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player={name}"
USER_AGENT = "osrs-quest-tracker/1.1 (personal tool for a friend group)"
HISTORY = os.environ.get("HISTORY", "history/collection_log.json")
WIKI_CACHE = os.environ.get("WIKI_CACHE", "history/wiki_cache.json")
WIKI_CACHE_TTL = 24 * 3600
WIKI_CACHE_VERSION = 2  # bump when the cached shape changes, so old caches are refetched
WIKI_IMAGES = "https://oldschool.runescape.wiki/images/"


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def api(**params):
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    return get_json(WIKI_API + "?" + urllib.parse.urlencode(params))


def page_html(title: str) -> str:
    return api(action="parse", page=title, prop="text")["parse"]["text"]


# ---------- tiny table extractor ----------
class TableParser(HTMLParser):
    """Collects every <table class="wikitable"> as rows of cells.

    Each cell is a list of tokens: ("text", str) or ("link", title, text).
    """

    def __init__(self):
        super().__init__()
        self.tables = []
        self._in_table = 0
        self._row = None
        self._cell = None
        self._link = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and "wikitable" in (a.get("class") or ""):
            self._in_table += 1
            self.tables.append([])
        elif not self._in_table:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "a" and self._cell is not None and a.get("title"):
            self._link = [a["title"], ""]

    def handle_endtag(self, tag):
        if not self._in_table:
            return
        if tag == "table":
            self._in_table -= 1
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif tag == "a" and self._link is not None:
            if self._cell is not None:
                self._cell.append(("link", self._link[0], self._link[1]))
            self._link = None

    def handle_data(self, data):
        if self._link is not None:
            self._link[1] += data
        elif self._cell is not None:
            self._cell.append(("text", data))


def cell_text(cell) -> str:
    return "".join(t[-1] for t in cell).strip()


def parse_qty_note(note: str):
    """'(3)' -> (3, False); '(3, acquired during quest)' -> (3, True); '' -> (None, False)."""
    m = re.search(r"\(([^)]*)\)", note)
    if not m:
        return None, False
    inner = m.group(1)
    acquired = "acquired" in inner
    q = re.search(r"[\d,]+", inner)
    qty = int(q.group(0).replace(",", "")) if q else None
    return qty, acquired


def fetch_items():
    """Parse 'Required Quest Item Totals' (members table = all quests)."""
    print("Fetching required-item totals...")
    p = TableParser()
    p.feed(page_html("Required Quest Item Totals"))
    tables = [t for t in p.tables if t and cell_text(t[0][0]).lower() == "item"]
    if len(tables) < 2:
        raise RuntimeError("Expected two item tables on the totals page")
    table = tables[-1]  # the members table includes free-to-play quests too

    items = []
    for row in table[1:]:
        if len(row) < 3:
            continue
        item_cell, qty_cell, needed_cell = row[0], row[1], row[2]
        links = [t for t in item_cell if t[0] == "link" and t[2].strip()]
        if not links:
            continue
        name = links[0][2].strip()
        page = links[0][1]
        text = cell_text(item_cell)
        total_txt = cell_text(qty_cell).replace(",", "")
        total = int(total_txt) if total_txt.isdigit() else None

        needed = []
        current = None
        for tok in needed_cell:
            if tok[0] == "link":
                if not tok[2].strip():
                    continue  # icon-only link
                current = {"page": tok[1], "qty": None, "acquired": False}
                needed.append(current)
            elif current is not None:
                qty, acquired = parse_qty_note(tok[1])
                if qty is not None:
                    current["qty"] = qty
                if acquired:
                    current["acquired"] = True
        items.append({
            "name": name,
            "page": page,
            "reusable": "reusable" in text,
            "total": total,
            "needed": needed,
        })
    print(f"  {len(items)} items")
    return items


def fetch_tradeability(items):
    """Mark each item tradeable True/False/None using wiki categories."""
    print("Fetching tradeability...")
    titles = sorted({it["page"] for it in items})
    result = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        data = api(
            action="query", prop="categories", redirects="1", cllimit="max",
            clcategories="Category:Tradeable items|Category:Untradeable items",
            titles="|".join(batch),
        )["query"]
        # requested title -> final title (normalisation + redirects)
        alias = {}
        for m in data.get("normalized", []) + data.get("redirects", []):
            alias[m["from"]] = m["to"]
        final_of = {t: alias.get(alias.get(t, t), alias.get(t, t)) for t in batch}
        cats = {}
        for pg in data.get("pages", []):
            names = {c["title"] for c in pg.get("categories", [])}
            if "Category:Tradeable items" in names:
                cats[pg["title"]] = True
            elif "Category:Untradeable items" in names:
                cats[pg["title"]] = False
            else:
                cats[pg["title"]] = None
        for t in batch:
            result[t] = cats.get(final_of[t])
        time.sleep(0.3)
    for it in items:
        it["tradeable"] = result.get(it["page"])
    known = sum(1 for it in items if it["tradeable"] is not None)
    print(f"  {known}/{len(items)} items classified")


def fetch_players():
    players = {}
    for name in PLAYERS:
        url = SYNC_URL.format(name=urllib.parse.quote(name))
        try:
            data = get_json(url)
            data.pop("timestamp", None)  # the response time, not a sync time; it would defeat the change check
            done = sum(1 for v in data.get("quests", {}).values() if v == 2)
            print(f"  {name}: {done} quests completed")
            players[name] = data
        except urllib.error.HTTPError as e:
            print(f"  {name}: HTTP {e.code} (no WikiSync data?)")
            players[name] = None
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: {e}")
            players[name] = None
        time.sleep(0.5)
    return players


def fetch_stats():
    """Levels and XP from the official hiscores.

    WikiSync only knows what it saw at the player's last login with the plugin,
    so the hiscores are the fresher source for levels; they also carry XP.
    Unranked accounts still return rows (rank -1), so a new group works fine.
    """
    stats = {}
    for name in PLAYERS:
        url = HISCORES_URL.format(name=urllib.parse.quote(name))
        try:
            data = get_json(url)
            skills = {
                s["name"]: {"level": s["level"], "xp": s["xp"]}
                for s in data.get("skills", [])
                if s.get("level", -1) >= 0
            }
            overall = skills.pop("Overall", None)
            # clue scrolls, minigames and boss kill counts, in the order the site lists them
            activities = {a["name"]: a["score"] for a in data.get("activities", [])}
            stats[name] = {"skills": skills, "overall": overall, "activities": activities}
            total = overall["level"] if overall else "?"
            xp = f"{overall['xp']:,}" if overall else "?"
            print(f"  {name}: total level {total}, {xp} xp")
        except urllib.error.HTTPError as e:
            print(f"  {name}: HTTP {e.code} (not on the hiscores?)")
            stats[name] = None
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: {e}")
            stats[name] = None
        time.sleep(0.5)
    return stats


def quest_titles(list_html):
    """Every quest and miniquest page title from the wiki's quest list."""
    p = TableParser()
    p.feed(list_html)
    titles = []
    for table in p.tables:
        heads = [cell_text(c).lower() for c in table[0]] if table else []
        col = next((i for i, h in enumerate(heads) if h.startswith("name")), None)
        if col is None:
            continue
        for row in table[1:]:
            links = [t for t in row[col] if t[0] == "link" and t[2].strip()] if len(row) > col else []
            if links:
                titles.append(links[0][1])
    return list(dict.fromkeys(titles))


SCP_RE = re.compile(r"\{\{SCP\|([^|}]+)\|(\d+)[^}]*\}\}(?:\s*\{\{Boostable\|(yes|no)[^}]*\}\})?", re.I)
# "the sum of your [[Attack]] and [[Strength]] must be at or above 130" -> a Attack+Strength requirement
SUM_RE = re.compile(r"sum of your \[\[(\w+)\]\] and \[\[(\w+)\]\] must be at or above (\d+)", re.I)


def infobox_field(name, wikitext):
    """One |field= of a quest infobox, up to the next field or the closing braces."""
    m = re.search(r"\|\s*%s\s*=(.*?)(?=\n\|\s*[a-z]+\s*=|\n\}\})" % name, wikitext, re.S | re.I)
    return m.group(1) if m else ""


def parse_reqs(wikitext):
    """Skill requirements from the infobox's |requirements= list.

    Each line is one requirement; a line offering alternatives ("40 Attack or
    40 Strength") is tagged so the page can treat it as any-of.
    """
    reqs = []
    for n, line in enumerate(infobox_field("requirements", wikitext).split("\n")):
        found = SCP_RE.findall(line)
        total = SUM_RE.search(line)
        if total:
            found.append((total.group(1) + "+" + total.group(2), total.group(3), ""))
        for skill, level, boost in found:
            req = {"skill": skill.strip(), "level": int(level), "boostable": boost.lower() == "yes"}
            if len(found) > 1 and re.search(r"\bor\b", line, re.I):
                req["or"] = n
            reqs.append(req)
    return reqs


def parse_item_skills(wikitext):
    """Skill levels named in the infobox's |items= list.

    These are never hard requirements - they are the levels to make or gather
    something you could also buy, find, or get as a drop (the lyre in The
    Fremennik Trials, say, which the requirements field never mentions). Only
    the highest level per skill is kept, which is what gathering the lot needs.
    """
    best = {}
    for skill, level, boost in SCP_RE.findall(infobox_field("items", wikitext)):
        skill, level = skill.strip(), int(level)
        if skill not in best or level > best[skill]["level"]:
            best[skill] = {"skill": skill, "level": level, "boostable": boost.lower() == "yes"}
    return sorted(best.values(), key=lambda r: (-r["level"], r["skill"]))


XP_RE = re.compile(r"\{\{SCP\|([A-Za-z ]+)\|([\d,]+)[^}]*\}\}", re.I)


def clean_wikitext(line: str) -> str:
    """A reward line as text, keeping [[links]] for the page to render."""
    line = re.sub(r"\{\{SCP\|([^|}]+)(?:\|[^}]*)?\}\}", r"\1", line)  # {{SCP|Attack|...}} -> Attack
    line = re.sub(r"\{\{[^{}]*\}\}", "", line)
    line = re.sub(r"\[\[File:[^\]]*\]\]", "", line)
    line = re.sub(r"<[^>]+>|'{2,3}", "", line)
    return re.sub(r"\s+", " ", line).strip(" *:")


def parse_quest_page(wikitext, quest_titles):
    """Direct prerequisite quests and rewards (xp per skill, and everything else as lines)."""
    prereqs = []
    # a quest link is a sub-prerequisite (skip it) when the bullet it nests under is itself a quest
    stack = []  # (depth, is_quest_line) of the enclosing bullets
    for line in infobox_field("requirements", wikitext).split("\n"):
        bullets = re.match(r"\s*(\*+)", line)
        if not bullets:
            continue
        depth = len(bullets.group(1))
        links = [t for t in re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", line) if t in quest_titles]
        while stack and stack[-1][0] >= depth:
            stack.pop()
        nested = bool(stack) and stack[-1][1]
        if links and not nested:
            prereqs.extend(t for t in links if t not in prereqs)
        stack.append((depth, bool(links)))
    xp, other = [], []
    body = rewards_block(wikitext)
    if body:
        for line in body.split("\n"):
            if not line.strip().startswith("*"):
                continue
            amounts = XP_RE.findall(line)
            if amounts and re.search(r"experience|\bxp\b", line, re.I):
                xp.extend([skill.strip(), int(n.replace(",", ""))] for skill, n in amounts)
            elif not re.search(r"quest point", line, re.I):
                text = clean_wikitext(line)
                if text:
                    other.append(text)
    # the page's own list of what needs this quest - a second source for the prerequisite graph
    required_for = []
    m = re.search(r"==\s*Required for completing\s*==(.*?)(?=\n==[^=]|\Z)", wikitext, re.S | re.I)
    if m:
        required_for = [t for t in re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", m.group(1)) if t in quest_titles]
    return {"prereqs": prereqs, "requiredFor": required_for, "xp": xp, "rewards": other,
            "itemSkills": parse_item_skills(wikitext)}


def rewards_block(wikitext):
    """The bullet list of rewards: the {{Quest rewards}} template's |rewards= (matched by
    brace depth, since the closing braces often share the last bullet's line, plus any
    bullets that spill out right after it), or a plain ==Rewards== section on miniquests."""
    start = wikitext.find("{{Quest rewards")
    if start >= 0:
        depth, i = 0, start
        while i < len(wikitext):
            if wikitext.startswith("{{", i):
                depth += 1
                i += 2
            elif wikitext.startswith("}}", i):
                depth -= 1
                i += 2
                if depth == 0:
                    break
            else:
                i += 1
        template = wikitext[start:i]
        r = re.search(r"\|\s*rewards\s*=(.*)", template, re.S)
        body = r.group(1)[:-2] if r else ""
        tail = re.match(r"((?:\s*\*[^\n]*\n?)+)", wikitext[i:])
        if tail:
            body += "\n" + tail.group(1)
        return body
    m = re.search(r"==\s*Rewards?\s*==(.*?)(?=\n==[^=]|\Z)", wikitext, re.S | re.I)
    return m.group(1) if m else ""


def fetch_reqs(titles):
    """Wikitext for every quest page: skill requirements, plus prerequisites and rewards."""
    known = set(titles)
    info = {}
    reqs = {}
    for i in range(0, len(titles), 20):
        batch = titles[i:i + 20]
        data = api(
            action="query", prop="revisions", rvprop="content", rvslots="main",
            redirects="1", titles="|".join(batch),
        )["query"]
        alias = {m["from"]: m["to"] for m in data.get("normalized", []) + data.get("redirects", [])}
        content = {}
        for pg in data.get("pages", []):
            if pg.get("revisions"):
                content[pg["title"]] = pg["revisions"][0]["slots"]["main"]["content"]
        for t in batch:
            final = alias.get(alias.get(t, t), alias.get(t, t))
            if final in content:
                reqs[t] = parse_reqs(content[final])
                info[t] = parse_quest_page(content[final], known)
        time.sleep(0.3)
    with_reqs = sum(1 for v in reqs.values() if v)
    with_rewards = sum(1 for v in info.values() if v["xp"] or v["rewards"])
    print(f"  {len(reqs)}/{len(titles)} quest pages, {with_reqs} with skill requirements, {with_rewards} with rewards")
    return reqs, info


def fetch_bytes(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.geturl()


def lookup_item(item_id: int):
    """Item name and icon (data URI) from the wiki, by id.

    The icon file is read from the item page's infobox rather than guessed from the
    name: variants and stackables are named "Cow slippers (1).png", "Purple sweets 1.png".
    """
    _, final = fetch_bytes(f"https://oldschool.runescape.wiki/w/Special:Lookup?type=item&id={item_id}")
    name = urllib.parse.unquote(final.split("/w/")[-1].split("#")[0].split("?")[0]).replace("_", " ")
    candidates = []
    try:
        page = api(action="query", prop="revisions", rvprop="content", rvslots="main", titles=name)
        wikitext = page["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
        candidates = re.findall(r"\|\s*image\d*\s*=\s*\[\[File:([^\]|]+)", wikitext)
    except Exception:  # noqa: BLE001
        pass
    candidates += [name + ".png", name + " 5.png", name + " 1.png"]
    icon = None
    for filename in candidates:
        try:
            blob, _ = fetch_bytes(WIKI_IMAGES + urllib.parse.quote(filename.strip().replace(" ", "_")))
            icon = "data:image/png;base64," + base64.b64encode(blob).decode()
            break
        except urllib.error.HTTPError:
            continue
    return name, icon


def update_clog_history(players):
    """First-seen time per collection log item per player, kept across runs.

    Items present the first time a player's log is seen get no time (they were
    gained before tracking began). Unknown ids are resolved to names and icons
    once and cached in the same file.
    """
    try:
        with open(HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        hist = {"players": {}, "items": {}}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for name in PLAYERS:
        data = players.get(name) or {}
        ids = [int(i) for i in (data.get("collection_log") or []) if isinstance(i, int)]
        if not ids:
            continue  # not synced (yet); keep what we have
        seen = hist["players"].setdefault(name, {})
        first_time = not seen
        for i in ids:
            seen.setdefault(str(i), None if first_time else now)
    known = {i for i, v in hist["items"].items() if v.get("icon")}  # retry anything still without an icon
    unresolved = sorted({i for p in hist["players"].values() for i in p} - known, key=int)
    for i in unresolved:
        try:
            item_name, icon = lookup_item(int(i))
            hist["items"][i] = {"name": item_name, "icon": icon}
        except Exception as e:  # noqa: BLE001
            print(f"  item {i}: {e}")
        time.sleep(0.3)
    os.makedirs(os.path.dirname(HISTORY) or ".", exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f)
    tracked = sum(len(p) for p in hist["players"].values())
    print(f"  {tracked} logged items across {len(hist['players'])} players, {len(unresolved)} newly resolved")
    # what the page needs: per player, items newest first (untimed ones last)
    out = {}
    for name, seen in hist["players"].items():
        rows = [{"id": int(i), "seen": t, **hist["items"].get(i, {"name": f"Item {i}", "icon": None})} for i, t in seen.items()]
        rows.sort(key=lambda r: (r["seen"] is not None, r["seen"] or ""), reverse=True)  # newest first, untimed last
        out[name] = rows[:40]
    return out


def fetch_ca_total():
    """How many combat achievement tasks exist, from the wiki's all-tasks table."""
    p = TableParser()
    p.feed(page_html("Combat Achievements/All tasks"))
    table = max(p.tables, key=len, default=[])
    return len([r for r in table[1:] if len(r) >= 3]) or None


def wiki_data(refresh: bool = False):
    """Everything that comes from the wiki, cached for a day."""
    if not refresh:
        try:
            with open(WIKI_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
            age = time.time() - cache.get("at", 0)
            if age < WIKI_CACHE_TTL and cache.get("v") == WIKI_CACHE_VERSION:
                print(f"Using wiki data cached {age / 3600:.1f} h ago")
                return cache
        except (OSError, ValueError):
            pass

    print("Fetching quest list...")
    list_html = page_html("Quests/List")

    items = fetch_items()
    fetch_tradeability(items)

    print("Fetching quest skill requirements...")
    reqs, quest_info = fetch_reqs(quest_titles(list_html))

    print("Fetching combat achievement count...")
    ca_total = fetch_ca_total()
    print(f"  {ca_total} tasks")

    cache = {"at": time.time(), "v": WIKI_CACHE_VERSION, "listHtml": list_html, "items": items,
             "reqs": reqs, "questInfo": quest_info, "caTotal": ca_total}
    os.makedirs(os.path.dirname(WIKI_CACHE) or ".", exist_ok=True)
    with open(WIKI_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    return cache


def main() -> int:
    wiki = wiki_data(refresh="--refresh" in sys.argv)

    print("Fetching player quests...")
    players = fetch_players()

    print("Fetching player stats...")
    stats = fetch_stats()

    print("Updating collection log history...")
    clog = update_clog_history(players)

    payload = {
        "listHtml": wiki["listHtml"],
        "items": wiki["items"],
        "players": players,
        "stats": stats,
        "caTotal": wiki["caTotal"],
        "reqs": wiki["reqs"],
        "questInfo": wiki["questInfo"],
        "clog": clog,
    }
    # everything but the timestamp: the deploy is skipped when this has not changed
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with open("data.hash", "w") as f:
        f.write(digest)
    payload["fetchedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    js = "window.QUEST_DATA = " + json.dumps(payload).replace("</", "<\\/") + ";\n"
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js)
    print(f"Wrote data.js ({len(js) // 1024} KB), hash {digest[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
