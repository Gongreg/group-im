#!/usr/bin/env python3
"""Fetch everything the tracker needs and write data.js.

  - Quest list (wiki page HTML, parsed in the browser)
  - Required items per quest (wiki page "Required Quest Item Totals", parsed here)
  - Tradeability of each item (wiki categories)
  - Quest completion per player (WikiSync; only for players with the plugin)
  - Levels, XP, clue counts and boss kill counts per player (official hiscores; live)
  - Number of combat achievement tasks (wiki), so the stats tab can show a fraction
  - Skill requirements per quest (from each quest page's infobox wikitext)
  - Collection log history: WikiSync lists the log in a fixed order, so when each item
    was gained is recorded here by diffing against the previous run (HISTORY file)

Usage: python3 fetch_data.py
Only stdlib is used.
"""
import base64
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


def parse_reqs(wikitext):
    """Skill requirements from the infobox's |requirements= list.

    Each line is one requirement; a line offering alternatives ("40 Attack or
    40 Strength") is tagged so the page can treat it as any-of.
    """
    m = re.search(r"\|\s*requirements\s*=(.*?)(?=\n\|\s*[a-z]+\s*=|\n\}\})", wikitext, re.S | re.I)
    if not m:
        return []
    reqs = []
    for n, line in enumerate(m.group(1).split("\n")):
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


def fetch_reqs(titles):
    """Wikitext for every quest page, reduced to its skill requirements."""
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
        time.sleep(0.3)
    with_reqs = sum(1 for v in reqs.values() if v)
    print(f"  {len(reqs)}/{len(titles)} quest pages, {with_reqs} with skill requirements")
    return reqs


def fetch_bytes(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.geturl()


def lookup_item(item_id: int):
    """Item name and icon (data URI) from the wiki, by id."""
    _, final = fetch_bytes(f"https://oldschool.runescape.wiki/w/Special:Lookup?type=item&id={item_id}")
    name = urllib.parse.unquote(final.split("/w/")[-1].split("#")[0].split("?")[0]).replace("_", " ")
    icon = None
    for suffix in ("", "_5", "_1"):  # stackables keep numbered icon files
        try:
            blob, _ = fetch_bytes(WIKI_IMAGES + urllib.parse.quote(name.replace(" ", "_")) + suffix + ".png")
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
    unresolved = sorted({i for p in hist["players"].values() for i in p} - set(hist["items"]), key=int)
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


def main() -> int:
    print("Fetching quest list...")
    list_html = page_html("Quests/List")

    items = fetch_items()
    fetch_tradeability(items)

    print("Fetching quest skill requirements...")
    reqs = fetch_reqs(quest_titles(list_html))

    print("Fetching combat achievement count...")
    ca_total = fetch_ca_total()
    print(f"  {ca_total} tasks")

    print("Fetching player quests...")
    players = fetch_players()

    print("Fetching player stats...")
    stats = fetch_stats()

    print("Updating collection log history...")
    clog = update_clog_history(players)

    payload = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "listHtml": list_html,
        "items": items,
        "players": players,
        "stats": stats,
        "caTotal": ca_total,
        "reqs": reqs,
        "clog": clog,
    }
    js = "window.QUEST_DATA = " + json.dumps(payload).replace("</", "<\\/") + ";\n"
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js)
    print(f"Wrote data.js ({len(js) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
