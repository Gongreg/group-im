#!/usr/bin/env python3
"""Fetch everything the tracker needs and write data.js.

  - Quest list (wiki page HTML, parsed in the browser)
  - Required items per quest (wiki page "Required Quest Item Totals", parsed here)
  - Tradeability of each item (wiki categories)
  - Quest completion per player (WikiSync; only for players with the plugin)
  - Levels and XP per player (official hiscores; live, unlike WikiSync)
  - Skill icons, inlined as data URIs so the page needs no external assets

Usage: python3 fetch_data.py
Only stdlib is used.
"""
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

PLAYERS = ["gaudyk", "uncle sacks", "Moms klit", "knysliukas", "cluescrollas"]

# in-game skill-tab order; index.html lays the stats table out the same way
SKILLS = [
    "Attack", "Hitpoints", "Mining", "Strength", "Agility", "Smithing",
    "Defence", "Herblore", "Fishing", "Ranged", "Thieving", "Cooking",
    "Prayer", "Crafting", "Firemaking", "Magic", "Fletching", "Woodcutting",
    "Runecraft", "Slayer", "Farming", "Construction", "Hunter", "Sailing",
]

WIKI_API = "https://oldschool.runescape.wiki/api.php"
WIKI_IMAGES = "https://oldschool.runescape.wiki/images/"
SYNC_URL = "https://sync.runescape.wiki/runelite/player/{name}/STANDARD"
HISCORES_URL = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player={name}"
USER_AGENT = "osrs-quest-tracker/1.1 (personal tool for a friend group)"


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
            skills = {
                s["name"]: {"level": s["level"], "xp": s["xp"]}
                for s in get_json(url).get("skills", [])
                if s.get("level", -1) >= 0
            }
            overall = skills.pop("Overall", None)
            stats[name] = {"skills": skills, "overall": overall}
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


def fetch_icons():
    """Skill icons from the wiki as data URIs (a few hundred bytes each)."""
    wanted = [(s, s.replace(" ", "_") + "_icon.png") for s in SKILLS]
    wanted.append(("Quest point", "Quest_point_icon.png"))
    icons = {}
    for key, filename in wanted:
        try:
            req = urllib.request.Request(WIKI_IMAGES + filename, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                blob = resp.read()
            icons[key] = "data:image/png;base64," + base64.b64encode(blob).decode()
        except Exception as e:  # noqa: BLE001
            print(f"  {filename}: {e}")
        time.sleep(0.1)
    print(f"  {len(icons)}/{len(wanted)} icons, {sum(map(len, icons.values())) // 1024} KB")
    return icons


def main() -> int:
    print("Fetching quest list...")
    list_html = page_html("Quests/List")

    items = fetch_items()
    fetch_tradeability(items)

    print("Fetching icons...")
    icons = fetch_icons()

    print("Fetching player quests...")
    players = fetch_players()

    print("Fetching player stats...")
    stats = fetch_stats()

    payload = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "listHtml": list_html,
        "items": items,
        "players": players,
        "stats": stats,
        "icons": icons,
    }
    js = "window.QUEST_DATA = " + json.dumps(payload).replace("</", "<\\/") + ";\n"
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js)
    print(f"Wrote data.js ({len(js) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
