"""Parse B.LEAGUE box score HTML and store total and per-period statistics."""
from __future__ import annotations

import re
import json
import importlib.util
import shutil
import sqlite3
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import datetime, date as date_type, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from bs4 import BeautifulSoup, Comment


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bleague_data.db"
HTML_DIR = BASE_DIR
PROFILE_POSITION_CACHE = {}

# Table order in the B.LEAGUE box score, including BSR (blocks received).
PLAYER_COLUMNS = [
    "jersey_number", "starter", "player_name", "position", "minutes",
    "points", "fgm", "fga", "fg_pct", "two_fgm", "two_fga", "two_fg_pct",
    "three_fgm", "three_fga", "three_fg_pct", "ftm", "fta", "ft_pct",
    "efg_pct", "ts_pct", "offensive_rebounds", "defensive_rebounds",
    "total_rebounds", "assists", "ast_to", "turnovers", "steals", "blocks",
    "blocks_received", "fouls", "fouls_drawn", "efficiency", "plus_minus",
]
INTEGER_FIELDS = set(PLAYER_COLUMNS) & {
    "jersey_number", "points", "fgm", "fga", "two_fgm", "two_fga",
    "three_fgm", "three_fga", "ftm", "fta", "offensive_rebounds",
    "defensive_rebounds", "total_rebounds", "assists", "turnovers", "steals",
    "blocks", "blocks_received", "fouls", "fouls_drawn", "efficiency", "plus_minus",
}
FLOAT_FIELDS = set(PLAYER_COLUMNS) & {
    "fg_pct", "two_fg_pct", "three_fg_pct", "ft_pct", "efg_pct", "ts_pct", "ast_to"
}
TEAM_VALUE_COLUMNS = [
    "minutes", "points", "fgm", "fga", "fg_pct", "two_fgm", "two_fga",
    "two_fg_pct", "three_fgm", "three_fga", "three_fg_pct", "ftm", "fta",
    "ft_pct", "efg_pct", "ts_pct", "offensive_rebounds", "defensive_rebounds",
    "total_rebounds", "assists", "ast_to", "turnovers", "steals", "blocks",
    "blocks_received", "fouls", "fouls_drawn", "efficiency", "plus_minus",
]


def text(el):
    if el is None:
        return None
    return re.sub(r"[\u200b\u200c\u200d\ufeff]", "", el.get_text(" ", strip=True)).strip() or None


def japanese_wovn(el):
    """Return Japanese text from the WOVN source comment immediately inside an element."""
    if el is None:
        return None
    for node in el.descendants:
        if isinstance(node, Comment):
            m = re.match(r"\s*wovn-src:(.*?)\s*$", str(node))
            if m and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", m.group(1)):
                return m.group(1).strip()
    return None


def cell_value(td):
    value = td.get("data-text") if td.has_attr("data-text") else text(td)
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in {"", "-", "－", "—", "–", "null", "\xa0"} else value


def number(value, integer=False):
    if value is None:
        return None
    try:
        n = float(str(value).replace(",", "").replace("%", "").strip())
        return int(n) if integer else n
    except (ValueError, TypeError):
        return None


def seconds(value):
    m = re.fullmatch(r"(\d+):(\d{2})", str(value or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else number(value, True)


def parse_player(row, team_name):
    cells = row.find_all("td", recursive=False)
    if len(cells) != len(PLAYER_COLUMNS):
        raise ValueError(f"選手行 {row.get('data-player-id')} の列数が想定外です: {len(cells)}")
    vals = dict(zip(PLAYER_COLUMNS, (cell_value(c) for c in cells)))
    name_el = row.select_one("td.PlayingFlg .for-pc")
    vals["player_name"] = japanese_wovn(name_el) or text(name_el)
    vals["jersey_number"] = number(vals["jersey_number"], True)
    vals["starter"] = 1 if vals["starter"] not in (None, "", "0", "null") else 0
    vals["minutes_seconds"] = seconds(vals["minutes"])
    for k in INTEGER_FIELDS:
        vals[k] = number(vals[k], True)
    for k in FLOAT_FIELDS:
        vals[k] = number(vals[k])
    return {"player_id": row.get("data-player-id", "").strip(), "team_name": team_name,
            "position": vals.pop("position"), **vals}


def row_stats(row, columns, start=0):
    """Read a footer row using data-column indices, which are stable despite colspan."""
    result = {}
    for td in row.find_all("td", recursive=False):
        col = td.get("data-column")
        if col is None:
            continue
        index = int(col)
        if index < start:
            continue
        # Table column 4 is playing time; statistical columns start at 5.
        if index == 4:
            result["minutes"] = cell_value(td)
        elif index >= 5:
            key_index = index - 5
            if key_index < len(columns):
                key = columns[key_index]
                raw = cell_value(td)
                result[key] = number(raw, key in INTEGER_FIELDS)
                if key in FLOAT_FIELDS:
                    result[key] = number(raw)
    return result


def parse_html(schedule_key):
    path = HTML_DIR / f"boxscore_html_{schedule_key}.html"
    if not path.exists():
        path = HTML_DIR / f"boxscore_html_{schedule_key}.txt"
    if not path.exists():
        raise FileNotFoundError(path)
    return parse_html_source(schedule_key, path.read_text(encoding="utf-8"), path.name)


def parse_html_source(schedule_key, source, source_file):
    soup = BeautifulSoup(source, "html.parser")
    title = text(soup.title)
    embedded = extract_embedded_context(source)
    if embedded:
        return parse_context_game(
            schedule_key, embedded, title, source_file, extract_embedded_positions(source)
        )
    dm = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})", title or "")
    game_date = f"{dm[1]}-{int(dm[2]):02d}-{int(dm[3]):02d}" if dm else None
    box = soup.select_one("#boxscore")
    if box is None:
        raise ValueError("#boxscore が見つかりません")
    periods = []
    for panel in box.select(".js-inner-tab-content[data-period]"):
        period = int(panel["data-period"])
        teams = []
        for table in panel.select("table.score-tab-table"):
            tbody = table.select_one("tbody.Home, tbody.Away")
            if tbody is None:
                continue
            side = "home" if "Home" in tbody.get("class", []) else "away"
            heading = table.find_previous("h2", class_="club-name")
            name_el = heading.select_one(".name") if heading else None
            team_name = japanese_wovn(name_el) or text(name_el)
            total = next((tr for tr in table.select("tfoot tr")
                          if (text(tr.select_one("td.foot-ttl")) or "").lower() == "total"), None)
            if total is None:
                raise ValueError(f"{team_name} period={period}: total 行がありません")
            stats = row_stats(total, TEAM_VALUE_COLUMNS[1:])
            players = [parse_player(r, team_name) for r in tbody.select("tr[data-player-id]")]
            teams.append({"team_name": team_name, "side": side, "score": stats.get("points"),
                          "stats": stats, "players": players})
        if len(teams) == 2:
            periods.append({"period": period, "teams": teams})
    if not periods or periods[0]["period"] != 0:
        raise ValueError("TOTAL (data-period=0) のチームデータがありません")
    total = periods[0]
    if len(total["teams"]) != 2 or {t["side"] for t in total["teams"]} != {"home", "away"}:
        raise ValueError("TOTAL のホーム・アウェイチームを特定できません")
    periods.sort(key=lambda p: p["period"])
    return {"schedule_key": schedule_key, "game_date": game_date, "page_title": title,
            "source_file": source_file, "league": league_from_title(title), "periods": periods}


def extract_embedded_context(source):
    """Read the official game's embedded Genius context JSON without executing JS."""
    marker = "_contexts_s3id.data = "
    pos = source.find(marker)
    if pos < 0:
        return None
    start = source.find("{", pos + len(marker))
    if start < 0:
        return None
    depth = 0
    quoted = escaped = False
    for end in range(start, len(source)):
        ch = source[end]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(source[start:end + 1])
    raise ValueError("埋め込み試合データのJSONが閉じていません")


def extract_embedded_positions(source):
    """Read the official page's _positions map (player ID -> position code)."""
    marker = re.search(r"\b_positions\s*=\s*", source)
    if not marker:
        return {}
    start = source.find("{", marker.end())
    if start < 0:
        return {}
    try:
        positions, _ = json.JSONDecoder().raw_decode(source[start:])
    except json.JSONDecodeError:
        return {}
    if not isinstance(positions, dict):
        return {}
    cleaned = {}
    for player_id, position in positions.items():
        value = str(position).strip()
        cleaned[str(player_id)] = None if value.lower() in {"", "&nbsp;", "-", "null"} else value
    return cleaned


def extract_profile_position(source, season):
    """Return a unique nonblank official position from a roster profile season row."""
    soup = BeautifulSoup(source, "html.parser")
    found = set()
    season_start = season.year if hasattr(season, "year") else int(season)
    if hasattr(season, "month") and season.month < 7:
        season_start -= 1
    season_text = f"{season_start}-{(season_start + 1) % 100:02d}"
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        position_index = None
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            labels = [cell.get_text(" ", strip=True).upper() for cell in cells]
            if "SEASON" in labels and "PO" in labels:
                position_index = labels.index("PO")
                break
        if position_index is None:
            continue
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            values = [cell.get_text(" ", strip=True) for cell in cells]
            if len(values) <= position_index or values[0] != season_text:
                continue
            position = values[position_index].strip()
            if position and position.upper() not in {"&NBSP;", "-", "NULL"}:
                found.add(position)
    return next(iter(found)) if len(found) == 1 else None


def fill_missing_positions_from_profiles(game, season):
    """Supplement blank game-page positions from the official profile for this season."""
    season_start = season.year if hasattr(season, "year") else int(season)
    if hasattr(season, "month") and season.month < 7:
        season_start -= 1
    season_label = f"{season_start}-{(season_start + 1) % 100:02d}"
    total_teams = next(period["teams"] for period in game["periods"] if period["period"] == 0)
    missing_ids = {
        str(player["player_id"])
        for team in total_teams for player in team["players"] if not player.get("position")
    }
    for player_id in missing_ids:
        cache_key = (player_id, season_label)
        if cache_key not in PROFILE_POSITION_CACHE:
            try:
                raw = fetch_url(f"https://www.bleague.jp/roster_detail/?PlayerID={player_id}")
                PROFILE_POSITION_CACHE[cache_key] = extract_profile_position(
                    raw.decode("utf-8", errors="replace"), season
                )
            except Exception as exc:
                PROFILE_POSITION_CACHE[cache_key] = None
                print(f"選手ID {player_id}: プロフィールからポジションを補えませんでした ({exc})")
        position = PROFILE_POSITION_CACHE[cache_key]
        if position:
            for period in game["periods"]:
                for team in period["teams"]:
                    for player in team["players"]:
                        if str(player["player_id"]) == player_id:
                            player["position"] = position


def league_from_title(title):
    m = re.search(r"B\.LEAGUE\s+(PREMIER|ONE|NEXT)", title or "", re.I)
    return f"B.{m.group(1).upper()}" if m else None


def parse_context_game(schedule_key, context, title, source_file, positions=None):
    game = context.get("Game") or {}
    actual_key = int(game.get("ScheduleKey") or context.get("ScheduleKey") or 0)
    if actual_key != schedule_key:
        raise ValueError(f"HTMLのScheduleKey={actual_key}、指定={schedule_key} が一致しません")
    if game.get("RecordFixedFlg") is not True or game.get("GameEndedFlg") is not True:
        raise ValueError(f"試合 {schedule_key} はまだ公式記録が確定していません")
    if game.get("BoxscoreExistsFlg") is not True:
        raise ValueError(f"試合 {schedule_key} に公式ボックススコアがありません")

    summary_periods = {int(x["PeriodCategory"]) for x in context.get("Summaries", [])
                       if str(x.get("PeriodCategory", "")).isdigit()}
    # 1-4 are regulation quarters; 5+ are OT periods. MaxPeriod only reports
    # regulation periods. Include any period category with its own score pair;
    # aggregate summary rows have no matching per-period score fields.
    period_ids = sorted(p for p in summary_periods
                        if p == 18 or (p >= 1 and game.get(f"HomeTeamScore{p:02d}") is not None
                                       and game.get(f"AwayTeamScore{p:02d}") is not None))
    if 18 not in period_ids:
        raise ValueError(f"試合 {schedule_key} のTOTAL集計がありません")
    total_id = 18
    sides = [("home", "Home"), ("away", "Away")]
    stats_by_period = {int(r["PeriodCategory"]): r for r in context.get("Summaries", [])
                       if str(r.get("PeriodCategory", "")).isdigit()}
    score_keys = {
        "home": {p: game.get(f"HomeTeamScore{p:02d}") for p in period_ids if p != 18},
        "away": {p: game.get(f"AwayTeamScore{p:02d}") for p in period_ids if p != 18},
    }
    score_keys["home"][18] = game.get("HomeTeamScore")
    score_keys["away"][18] = game.get("AwayTeamScore")
    teams_for_period = {p: [] for p in [0] + [n for n in period_ids if n != 18]}
    players_all = {side: [] for side, _ in sides}
    for side, prefix in sides:
        team_name = game.get(f"{prefix}TeamNameJ")
        for raw in context.get(f"{prefix}Boxscores", []):
            if raw.get("Category") != 1 or not raw.get("PlayerID"):
                continue
            cat = int(raw.get("PeriodCategory", 0))
            if cat == 18:
                period = 0
            elif cat in period_ids:
                period = cat
            else:
                continue
            mins = raw.get("PlayTime") or None
            p = {
                "player_id": str(raw["PlayerID"]), "team_name": team_name,
                "jersey_number": number(raw.get("PlayerNo"), True),
                "player_name": raw.get("PlayerNameJ") or raw.get("PlayerNameE"),
                "position": (positions or {}).get(str(raw["PlayerID"])),
                "starter": int(raw.get("StartingFlg") in (1, True)),
                "minutes": mins, "minutes_seconds": seconds(mins),
                "points": number(raw.get("Point"), True),
                "two_fgm": number(raw.get("PT2M"), True), "two_fga": number(raw.get("PT2A"), True),
                "three_fgm": number(raw.get("PT3M"), True), "three_fga": number(raw.get("PT3A"), True),
                "ftm": number(raw.get("FTM"), True), "fta": number(raw.get("FTA"), True),
                "offensive_rebounds": number(raw.get("RB_OFF"), True),
                "defensive_rebounds": number(raw.get("RB_DEF"), True),
                "total_rebounds": number(raw.get("RB_TOT"), True),
                "assists": number(raw.get("AS"), True), "ast_to": number(raw.get("AST_TO")),
                "turnovers": number(raw.get("TO"), True), "steals": number(raw.get("ST"), True),
                "blocks": number(raw.get("BS"), True), "blocks_received": number(raw.get("BSON"), True),
                "fouls": number(raw.get("FOUL"), True), "fouls_drawn": number(raw.get("FOULON"), True),
                "efficiency": number(raw.get("EFF"), True), "plus_minus": number(raw.get("PLUSMINUS"), True),
                "fgm": number(raw.get("PT2M"), True) + number(raw.get("PT3M"), True),
                "fga": number(raw.get("PT2A"), True) + number(raw.get("PT3A"), True),
            }
            # Rates in Genius JSON are fractions for player EFG and TS; the DB stores percent units.
            p["fg_pct"] = 100 * p["fgm"] / p["fga"] if p["fga"] else 0.0
            p["two_fg_pct"] = 100 * p["two_fgm"] / p["two_fga"] if p["two_fga"] else 0.0
            p["three_fg_pct"] = 100 * p["three_fgm"] / p["three_fga"] if p["three_fga"] else 0.0
            p["ft_pct"] = 100 * p["ftm"] / p["fta"] if p["fta"] else 0.0
            p["efg_pct"] = number(raw.get("EFG")) * 100 if raw.get("EFG") is not None else None
            p["ts_pct"] = number(raw.get("TS")) * 100 if raw.get("TS") is not None else None
            players_all[side].append((period, p))

        for summary_period, out_period in [(n, 0 if n == 18 else n) for n in period_ids]:
            summary = stats_by_period[summary_period]
            prefix = "HomeTeam" if side == "home" else "AwayTeam"
            fgm = number(summary.get(prefix + "PTM"), True)
            fga = number(summary.get(prefix + "PTA"), True)
            two_m, two_a = number(summary.get(prefix + "PT2M"), True), number(summary.get(prefix + "PT2A"), True)
            three_m, three_a = number(summary.get(prefix + "PT3M"), True), number(summary.get(prefix + "PT3A"), True)
            ftm, fta = number(summary.get(prefix + "FTM"), True), number(summary.get(prefix + "FTA"), True)
            score = number(score_keys[side].get(summary_period), True)
            if score is None:
                score = number(summary.get(prefix + "PT2M"), True) * 2 + number(summary.get(prefix + "PT3M"), True) * 3 + ftm
            total_minutes = sum(10 if p <= 4 else 5 for p in period_ids if p != 18) * 5
            minutes = f"{(total_minutes if summary_period == 18 else (10 if summary_period <= 4 else 5) * 5)}:00"
            stats = {
                "minutes": minutes, "points": score, "fgm": fgm, "fga": fga,
                "fg_pct": number(summary.get(prefix + "PTR")),
                "two_fgm": two_m, "two_fga": two_a, "two_fg_pct": number(summary.get(prefix + "PT2R")),
                "three_fgm": three_m, "three_fga": three_a, "three_fg_pct": number(summary.get(prefix + "PT3R")),
                "ftm": ftm, "fta": fta, "ft_pct": number(summary.get(prefix + "FTR")),
                "efg_pct": None, "ts_pct": None,
                "offensive_rebounds": number(summary.get(prefix + "RB_OFF"), True),
                "defensive_rebounds": number(summary.get(prefix + "RB_DEF"), True),
                "total_rebounds": number(summary.get(prefix + "RB_TOT"), True),
                "assists": number(summary.get(prefix + "AS"), True),
                "ast_to": (number(summary.get(prefix + "AS")) / number(summary.get(prefix + "TO"))
                           if number(summary.get(prefix + "TO")) else None),
                "turnovers": number(summary.get(prefix + "TO"), True),
                "steals": number(summary.get(prefix + "ST"), True),
                "blocks": number(summary.get(prefix + "BS"), True),
                "blocks_received": number(summary.get(prefix + "BSON"), True),
                "fouls": number(summary.get(prefix + "FOUL"), True),
                "fouls_drawn": number(summary.get(prefix + "FOULON"), True),
                "efficiency": None, "plus_minus": None,
            }
            team = {"team_name": team_name, "side": side, "score": score, "stats": stats,
                    "players": [p for per, p in players_all[side] if per == out_period]}
            teams_for_period[out_period].append(team)
    periods = [{"period": p, "teams": teams_for_period[p]} for p in sorted(teams_for_period)]
    if any(len(p["teams"]) != 2 for p in periods):
        raise ValueError(f"試合 {schedule_key}: 期間別にホーム・アウェイ両チームがそろいません")
    # GameDateTime is the official scheduled tip-off timestamp (Unix epoch).
    # The embedded context's `ymd` can be stale on some completed game pages.
    game_date = None
    game_datetime = game.get("GameDateTime")
    if game_datetime:
        try:
            game_date = datetime.fromtimestamp(int(game_datetime), tz=ZoneInfo("Asia/Tokyo")).date().isoformat()
        except (ValueError, TypeError, OSError, OverflowError):
            pass
    if game_date is None:
        dm = re.search(r"(20\d{2})-(\d{2})-(\d{2})", context.get("ymd", ""))
        game_date = f"{dm[1]}-{dm[2]}-{dm[3]}" if dm else None
    return {"schedule_key": schedule_key, "game_date": game_date, "page_title": title,
            "source_file": source_file, "league": league_from_title(title), "periods": periods}


def create_schema(conn):
    """Create the minimal schema: game metadata and period rows (0 means TOTAL)."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS games (
      schedule_key INTEGER PRIMARY KEY, game_date TEXT, page_title TEXT,
      home_team_name TEXT, away_team_name TEXT, home_score INTEGER, away_score INTEGER,
      source_file TEXT, updated_at TEXT, league TEXT);
    CREATE TABLE IF NOT EXISTS team_period_stats (
      schedule_key INTEGER NOT NULL, period INTEGER NOT NULL, side TEXT NOT NULL,
      team_name TEXT NOT NULL, score INTEGER, minutes TEXT,
      fgm INTEGER, fga INTEGER, fg_pct REAL, two_fgm INTEGER, two_fga INTEGER, two_fg_pct REAL,
      three_fgm INTEGER, three_fga INTEGER, three_fg_pct REAL, ftm INTEGER, fta INTEGER, ft_pct REAL,
      efg_pct REAL, ts_pct REAL, offensive_rebounds INTEGER, defensive_rebounds INTEGER,
      total_rebounds INTEGER, assists INTEGER, ast_to REAL, turnovers INTEGER, steals INTEGER,
      blocks INTEGER, blocks_received INTEGER, fouls INTEGER, fouls_drawn INTEGER,
      efficiency INTEGER, plus_minus INTEGER,
      PRIMARY KEY(schedule_key,period,side),
      FOREIGN KEY(schedule_key) REFERENCES games(schedule_key) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS player_period_stats (
      schedule_key INTEGER NOT NULL, period INTEGER NOT NULL, player_id TEXT NOT NULL,
      team_name TEXT NOT NULL, jersey_number INTEGER, player_name TEXT, position TEXT,
      starter INTEGER, minutes TEXT, minutes_seconds INTEGER, points INTEGER,
      fgm INTEGER, fga INTEGER, fg_pct REAL, two_fgm INTEGER, two_fga INTEGER, two_fg_pct REAL,
      three_fgm INTEGER, three_fga INTEGER, three_fg_pct REAL, ftm INTEGER, fta INTEGER,
      ft_pct REAL, efg_pct REAL, ts_pct REAL, offensive_rebounds INTEGER,
      defensive_rebounds INTEGER, total_rebounds INTEGER, assists INTEGER, ast_to REAL,
      turnovers INTEGER, steals INTEGER, blocks INTEGER, blocks_received INTEGER,
      fouls INTEGER, fouls_drawn INTEGER, efficiency INTEGER, plus_minus INTEGER,
      PRIMARY KEY(schedule_key,period,player_id),
      FOREIGN KEY(schedule_key) REFERENCES games(schedule_key) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS idx_player_period_name ON player_period_stats(player_name);
    CREATE INDEX IF NOT EXISTS idx_player_period_position ON player_period_stats(position);
    CREATE TABLE IF NOT EXISTS official_player_season_stats (
      season_year INTEGER NOT NULL, league TEXT NOT NULL, player_id TEXT NOT NULL,
      team_name TEXT, player_name TEXT, position TEXT, games_played INTEGER NOT NULL,
      minutes_seconds INTEGER, points INTEGER, fgm INTEGER, fga INTEGER,
      two_fgm INTEGER, two_fga INTEGER, three_fgm INTEGER, three_fga INTEGER,
      ftm INTEGER, fta INTEGER, offensive_rebounds INTEGER,
      defensive_rebounds INTEGER, total_rebounds INTEGER, assists INTEGER,
      turnovers INTEGER, steals INTEGER, blocks INTEGER, blocks_received INTEGER,
      fouls INTEGER, fouls_drawn INTEGER, efficiency INTEGER, plus_minus INTEGER,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(season_year, league, player_id));
    CREATE TABLE IF NOT EXISTS season_eff_reconciliation (
      season_year INTEGER NOT NULL, league TEXT NOT NULL, player_id TEXT NOT NULL,
      player_name TEXT, team_name TEXT, official_efficiency INTEGER NOT NULL,
      game_efficiency INTEGER NOT NULL, difference INTEGER NOT NULL,
      checked_at TEXT NOT NULL,
      PRIMARY KEY(season_year, league, player_id));
    CREATE INDEX IF NOT EXISTS idx_official_season_league ON official_player_season_stats(season_year, league);
    """)
    game_columns = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    if "league" not in game_columns:
        conn.execute("ALTER TABLE games ADD COLUMN league TEXT")


def insert_dict(conn, table, data, keys):
    cols = [k for k in keys if k in data]
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(':'+k for k in cols)})", data)


def save_to_database(game):
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        total = game["periods"][0]["teams"]
        home, away = next(t for t in total if t["side"] == "home"), next(t for t in total if t["side"] == "away")
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute("""INSERT INTO games (
              schedule_key,game_date,page_title,home_team_name,away_team_name,
              home_score,away_score,source_file,updated_at,league
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(schedule_key) DO UPDATE SET game_date=excluded.game_date,
              page_title=excluded.page_title,home_team_name=excluded.home_team_name,
              away_team_name=excluded.away_team_name,home_score=excluded.home_score,
              away_score=excluded.away_score,source_file=excluded.source_file,
              updated_at=excluded.updated_at,league=excluded.league""",
              (game["schedule_key"], game["game_date"], game["page_title"], home["team_name"],
               away["team_name"], home["score"], away["score"], game["source_file"], now, game.get("league")))
            for table in ("team_period_stats", "player_period_stats"):
                conn.execute(f"DELETE FROM {table} WHERE schedule_key=?", (game["schedule_key"],))
            player_keys = ["schedule_key", "player_id", "team_name", "jersey_number", "player_name", "position",
                           "starter", "minutes", "minutes_seconds"] + PLAYER_COLUMNS[5:]
            team_keys = ["schedule_key", "period", "side", "team_name", "score", "minutes"] + TEAM_VALUE_COLUMNS[2:]
            for period in game["periods"]:
                for team in period["teams"]:
                    common = {"schedule_key": game["schedule_key"], "period": period["period"],
                              "side": team["side"], "team_name": team["team_name"],
                              **team["stats"], "score": team["score"]}
                    insert_dict(conn, "team_period_stats", common, team_keys)
                    for player in team["players"]:
                        record = {"schedule_key": game["schedule_key"], "period": period["period"], **player}
                        insert_dict(conn, "player_period_stats", record, ["schedule_key", "period"] + player_keys[1:])
    finally:
        conn.close()
    print(f"Saved {game['schedule_key']}: {home['team_name']} {home['score']} - {away['score']} {away['team_name']}")
    print(f"Periods: {[p['period'] for p in game['periods']]}; players in TOTAL: {sum(len(t['players']) for t in total)}")


def fetch_url(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BLeagueStatsCollector/1.0)"})
    with urlopen(request, timeout=40) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return response.read()


def collect_date(date_text, delay_seconds=0.6):
    """Discover games in all three leagues, fetch boxscores, and import fixed results."""
    try:
        target = date_type.fromisoformat(date_text)
    except ValueError as exc:
        raise ValueError("日付は YYYY-MM-DD 形式で指定してください") from exc
    # `tab` chooses the league; leave the event filter empty to include all games.
    league_pages = [("B.PREMIER", "1"), ("B.ONE", "2"), ("B.NEXT", "3")]
    games = []
    for league, tab in league_pages:
        query = urlencode({"year": target.year, "mon": f"{target.month:02d}", "day": f"{target.day:02d}",
                           "event": "", "club": "", "tab": tab, "ha": "", "fb": ""})
        schedule_html = fetch_url(f"https://www.bleague.jp/schedule/?{query}").decode("utf-8", errors="replace")
        schedule_soup = BeautifulSoup(schedule_html, "html.parser")
        visible_dates, current_dates = schedule_page_dates(schedule_soup, target.year)
        if not visible_dates:
            raise RuntimeError(f"{league} の日程ページで日付表示を確認できません")
        if target.isoformat() not in visible_dates:
            print(f"{date_text}: {league} に対象日の試合日程がありません")
            continue
        if target.isoformat() not in current_dates:
            raise RuntimeError(
                f"{league} の日程選択日が対象日と不一致です: "
                f"対象={date_text}, 表示={sorted(current_dates)}"
            )
        league_ids = []
        for link in schedule_soup.select('a[href*="ScheduleKey="][href*="tab=4"]'):
            match = re.search(r"ScheduleKey=(\d+)", link.get("href", ""))
            if match and match.group(1) not in league_ids:
                league_ids.append(match.group(1))
        games.extend((key, league) for key in league_ids)
    by_key = {}
    for key, league in games:
        if key in by_key and by_key[key] != league:
            raise ValueError(f"ScheduleKey {key} が複数のリーグ日程にあります")
        by_key[key] = league
    games = list(by_key.items())
    ids = [key for key, _ in games]
    if not ids:
        print(f"{date_text}: 3リーグの日程にボックススコアへのリンクがありませんでした")
        return []

    imported, skipped, errors = [], [], []
    for index, (key_text, expected_league) in enumerate(games):
        key = int(key_text)
        try:
            detail_url = f"https://www.bleague.jp/game_detail/?ScheduleKey={key}&tab=4"
            raw = fetch_url(detail_url)
            game = parse_html_source(key, raw.decode("utf-8", errors="replace"), f"ScheduleKey={key}")
            if game["game_date"] != target.isoformat():
                raise ValueError(f"ページの日付が対象日と異なります: {game['game_date']}")
            if game.get("league") != expected_league:
                raise ValueError(f"日程={expected_league}、詳細ページ={game.get('league')} が一致しません")
            fill_missing_positions_from_profiles(game, target)
            missing_positions = [
                f"{player['player_name']}({player['player_id']})"
                for period in game["periods"] if period["period"] == 0
                for team in period["teams"] for player in team["players"]
                if not player.get("position")
            ]
            if missing_positions:
                print(f"試合ID {key}: ポジション未取得 " + ", ".join(missing_positions))
            save_to_database(game)
            imported.append(key)
        except Exception as exc:
            errors.append((key, str(exc)))
            print(f"試合ID {key}: 取込を見送りました ({exc})")
        if index + 1 < len(ids):
            time.sleep(delay_seconds)
    print(f"{date_text}: 日程リンク{len(ids)}件、取込{len(imported)}件、見送り{len(errors)}件")
    if errors:
        print("未取込ID:", ", ".join(str(k) for k, _ in errors))
        raise RuntimeError(f"{len(errors)}件の試合を取り込めませんでした")
    return imported


def schedule_page_dates(soup, year):
    """Return dates listed by the schedule calendar and its selected date(s)."""
    visible_dates, current_dates = set(), set()
    for item in soup.select(".js-schedule-date-slider-item"):
        month_el = item.select_one(".month")
        day_el = item.select_one(".day")
        if month_el is None or day_el is None:
            continue
        month_match = re.search(r"(\d{1,2})\s*/", month_el.get_text(" ", strip=True))
        day_match = re.search(r"\d{1,2}", day_el.get_text(" ", strip=True))
        if not month_match or not day_match:
            continue
        value = f"{year}-{int(month_match.group(1)):02d}-{int(day_match.group(0)):02d}"
        visible_dates.add(value)
        if "is-current" in item.get("class", []):
            current_dates.add(value)
    return visible_dates, current_dates


def get_daily_checkpoint():
    """Read the last fully checked date, initializing from existing game data."""
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS daily_ingest_state (
                state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            row = conn.execute(
                "SELECT state_value FROM daily_ingest_state WHERE state_key='last_checked_date'"
            ).fetchone()
            if row:
                return date_type.fromisoformat(row[0])
            latest = conn.execute("SELECT MAX(game_date) FROM games").fetchone()[0]
            if latest:
                return date_type.fromisoformat(latest)
        # Initial baseline for the 2026-27 season; update this at the next season start.
        return date_type(2026, 9, 21)
    finally:
        conn.close()


def set_daily_checkpoint(value):
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS daily_ingest_state (
                state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            conn.execute("""INSERT INTO daily_ingest_state(state_key,state_value,updated_at)
                VALUES('last_checked_date',?,?)
                ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,
                    updated_at=excluded.updated_at""", (value.isoformat(), datetime.now().isoformat(timespec="seconds")))
    finally:
        conn.close()


def run_daily():
    """Catch up, refresh the last seven days, then sync official season aggregates."""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    end_date = today - timedelta(days=1)
    last_checked = get_daily_checkpoint()
    # Revisit a seven-calendar-day window to pick up delayed official corrections.
    # Dates beyond the previous checkpoint are also covered by this same pass.
    refresh_start = max(date_type(2026, 9, 21), last_checked - timedelta(days=6))
    next_date = refresh_start
    while next_date <= end_date:
        target = next_date.isoformat()
        already_checked = next_date <= last_checked
        print(f"日次取込: {target} を{'再確認' if already_checked else '確認'}します")
        try:
            collect_date(target)
        except Exception as exc:
            print(f"日次取込エラー: {target}: {exc}", file=sys.stderr)
            print(f"チェックポイントは {last_checked} のままです。次回起動時に再試行します。", file=sys.stderr)
            return 1
        if next_date > last_checked:
            set_daily_checkpoint(next_date)
            last_checked = next_date
        next_date += timedelta(days=1)
    try:
        season_year = end_date.year if end_date.month >= 7 else end_date.year - 1
        official_rows = fetch_official_season_totals(season_year)
        save_official_season_totals(season_year, official_rows)
        differences = reconcile_season_efficiency(season_year)
    except Exception as exc:
        print(f"公式シーズン集計の更新に失敗しました: {exc}", file=sys.stderr)
        return 1
    print(f"日次取込完了: {last_checked} まで確認。公式シーズン集計 {len(official_rows)}選手分を更新しました")
    if differences:
        print(f"EFF照合アラート: {len(differences)}選手で公式シーズン合計とDBの試合別合計に差があります")
        for row in differences:
            print(f"  {row['league']} | {row['player_name']} ({row['team_name']}): "
                  f"公式 {row['official_efficiency']} / DB {row['game_efficiency']} "
                  f"(DB−公式 {row['difference']:+d})")
    else:
        print("EFF照合: 公式シーズン合計とDBの試合別合計に差はありません")
    try:
        snapshot_today_prediction()
    except Exception as exc:
        print(f"本日の試合前予想は保存できませんでした（アプリ起動後に再試行します）: {exc}", file=sys.stderr)
    return 0


def snapshot_today_prediction():
    """Freeze today's lineup from pre-today game data during the daily update."""
    app_path = BASE_DIR / "eff_app" / "app.py"
    if not app_path.is_file():
        raise FileNotFoundError(f"EFFアプリが見つかりません: {app_path}")
    module_name = "bleague_eff_app_for_prediction_snapshot"
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("EFFアプリを読み込めません")
    app = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = app
    spec.loader.exec_module(app)
    if not app.get_today_games():
        print("本日は公式日程に試合がないため、予想は保存しません")
        return
    today_lineup = app.get_rankings(DB_PATH, "today", "all").get("prediction_lineup")
    if today_lineup:
        print(f"本日の試合前予想を保存しました（EFF合計 {today_lineup['efficiency_total']}）")
    else:
        print("本日の試合前予想は、対象選手または試合日程がないため保存されませんでした")


OFFICIAL_LEAGUE_QUERY = {
    "B.PREMIER": ("1", "player-b1", "2"),
    "B.ONE": ("2", "player-b2", "7"),
    "B.NEXT": ("3", "player-b3", "15"),
}


def parse_play_time(value):
    match = re.fullmatch(r"(\d+):(\d{2})", str(value or ""))
    return int(match[1]) * 60 + int(match[2]) if match else None


def parse_official_stat_row(html, league, season_year):
    soup = BeautifulSoup(html, "html.parser")
    row = soup.find("tr")
    cells = row.find_all("td", recursive=False) if row else []
    if len(cells) < 32:
        raise ValueError("公式選手集計の列数が想定と異なります")
    player_link = soup.select_one('a[href*="PlayerID="]')
    if player_link is None:
        raise ValueError("公式選手集計から選手IDを取得できません")
    id_match = re.search(r"PlayerID=(\d+)", player_link.get("href", ""))
    if not id_match:
        raise ValueError("公式選手集計の選手IDが不正です")
    player_info = cells[1]
    name = text(player_link)
    position = player_info.select_one(".table-player-pos")
    team = cells[2].get_text(" ", strip=True)
    raw = [cell.get_text(" ", strip=True).replace(",", "") for cell in cells]
    def integer(index):
        return int(raw[index]) if raw[index].isdigit() else 0
    return {
        "season_year": season_year, "league": league, "player_id": id_match[1],
        "team_name": team, "player_name": name,
        "position": position.get_text(" ", strip=True) if position else None,
        "games_played": integer(3), "minutes_seconds": parse_play_time(raw[5]),
        "points": integer(7), "fgm": integer(8), "fga": integer(9),
        "two_fgm": integer(11), "two_fga": integer(12),
        "three_fgm": integer(14), "three_fga": integer(15),
        "ftm": integer(17), "fta": integer(18),
        "offensive_rebounds": integer(20), "defensive_rebounds": integer(21),
        "total_rebounds": integer(22), "assists": integer(23),
        "turnovers": integer(24), "steals": integer(25), "blocks": integer(26),
        "blocks_received": integer(27), "fouls": integer(28),
        "fouls_drawn": integer(29), "efficiency": integer(30),
        "plus_minus": integer(31),
    }


def fetch_official_season_totals(season_year):
    """Fetch every player's official TOTAL row from B.LEAGUE's paged stats endpoint."""
    records = []
    for league, (tab, target, event) in OFFICIAL_LEAGUE_QUERY.items():
        index = 0
        league_rows = []
        seen_ids = set()
        while True:
            params = urlencode({
                "data_format": "json", "year": str(season_year), "tab": tab,
                "target": target, "value": "EFF", "o": "desc", "e": event,
                "dt": "tot", "index": str(index),
            })
            url = f"https://www.bleague.jp/stats/?{params}"
            payload = json.loads(fetch_url(url).decode("utf-8", errors="replace"))
            topics = payload.get("topics")
            if not isinstance(topics, list):
                raise ValueError(f"{league} の公式集計応答が不正です")
            for html in topics:
                row = parse_official_stat_row(html, league, season_year)
                if row["games_played"] <= 0:
                    continue
                if row["player_id"] in seen_ids:
                    raise ValueError(f"{league} の公式集計に選手ID重複があります: {row['player_id']}")
                seen_ids.add(row["player_id"])
                league_rows.append(row)
            next_index = payload.get("index")
            if not next_index:
                break
            next_index = int(next_index)
            if next_index <= index:
                raise ValueError(f"{league} の公式集計ページ送りが進みません")
            index = next_index
        if not league_rows:
            raise ValueError(f"{league} の公式シーズン集計が空でした")
        print(f"公式集計取得: {league} {len(league_rows)}選手")
        records.extend(league_rows)
    return records


OFFICIAL_STAT_KEYS = [
    "season_year", "league", "player_id", "team_name", "player_name", "position",
    "games_played", "minutes_seconds", "points", "fgm", "fga", "two_fgm", "two_fga",
    "three_fgm", "three_fga", "ftm", "fta", "offensive_rebounds", "defensive_rebounds",
    "total_rebounds", "assists", "turnovers", "steals", "blocks", "blocks_received",
    "fouls", "fouls_drawn", "efficiency", "plus_minus",
]


def save_official_season_totals(season_year, records):
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute("DELETE FROM official_player_season_stats WHERE season_year=?", (season_year,))
            conn.executemany(
                f"INSERT INTO official_player_season_stats ({','.join(OFFICIAL_STAT_KEYS)},updated_at) "
                f"VALUES ({','.join(':'+key for key in OFFICIAL_STAT_KEYS)},:updated_at)",
                [{**row, "updated_at": now} for row in records],
            )
    finally:
        conn.close()


def reconcile_season_efficiency(season_year):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        create_schema(conn)
        season_start = f"{season_year}-07-01"
        season_end = f"{season_year + 1}-07-01"
        team_rows = conn.execute("""
            SELECT p.player_id,g.league,p.team_name,COUNT(*) AS games_played,
                   SUM(COALESCE(p.efficiency,0)) AS game_efficiency
            FROM player_period_stats p JOIN games g USING(schedule_key)
            WHERE p.period=0 AND COALESCE(p.minutes_seconds,0)>0
              AND g.game_date>=? AND g.game_date<?
            GROUP BY p.player_id,g.league,p.team_name
        """, (season_start, season_end)).fetchall()
        by_player: dict[tuple[str, str], list[dict]] = {}
        for row in team_rows:
            by_player.setdefault((str(row["player_id"]), row["league"]), []).append(dict(row))

        official_rows = conn.execute("""
            SELECT league,player_id,player_name,team_name,games_played,efficiency
            FROM official_player_season_stats WHERE season_year=?
        """, (season_year,)).fetchall()
        differences = []
        unpaired_multi_team = 0
        for official in official_rows:
            team_splits = by_player.get((str(official["player_id"]), official["league"]), [])
            if len(team_splits) > 1:
                # Official season rows are club-specific after a midseason move.
                # Match by GP only when exactly one DB club split has that GP.
                candidates = [row for row in team_splits
                              if row["games_played"] == official["games_played"]]
                if len(candidates) != 1:
                    unpaired_multi_team += 1
                    continue
                raw_game_efficiency = candidates[0]["game_efficiency"]
            else:
                raw_game_efficiency = team_splits[0]["game_efficiency"] if team_splits else 0
            game_efficiency = max(0, raw_game_efficiency)
            official_efficiency = official["efficiency"]
            if game_efficiency != official_efficiency:
                differences.append({
                    "league": official["league"], "player_id": official["player_id"],
                    "player_name": official["player_name"], "team_name": official["team_name"],
                    "official_efficiency": official_efficiency,
                    "game_efficiency": game_efficiency,
                    "difference": game_efficiency - official_efficiency,
                })
        if unpaired_multi_team:
            print(f"EFF照合: 複数クラブ出場のうち公式出場試合数でクラブを一意に対応できない選手 {unpaired_multi_team}人は照合対象外です")
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute("DELETE FROM season_eff_reconciliation WHERE season_year=?", (season_year,))
            conn.executemany("""INSERT INTO season_eff_reconciliation
                (season_year,league,player_id,player_name,team_name,official_efficiency,
                 game_efficiency,difference,checked_at)
                VALUES (:season_year,:league,:player_id,:player_name,:team_name,
                        :official_efficiency,:game_efficiency,:difference,:checked_at)""",
                [{**row,"season_year":season_year,"checked_at":now} for row in differences])
        return differences
    finally:
        conn.close()


def delete_game_date(date_text):
    """Remove all normalized period rows for a date."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        keys = [r[0] for r in conn.execute("SELECT schedule_key FROM games WHERE game_date=?", (date_text,))]
        if not keys:
            print(f"{date_text}: 削除対象の試合データはありません")
            return 0
        marks = ",".join("?" for _ in keys)
        with conn:
            for table in ("team_period_stats", "player_period_stats"):
                conn.execute(f"DELETE FROM {table} WHERE schedule_key IN ({marks})", keys)
            conn.execute(f"DELETE FROM games WHERE schedule_key IN ({marks})", keys)
        print(f"{date_text}: {len(keys)}試合分の旧データを削除しました")
        return len(keys)
    finally:
        conn.close()


def main():
    global DB_PATH, HTML_DIR
    if len(sys.argv) >= 2 and sys.argv[1] == "--daily":
        return run_daily()
    if len(sys.argv) >= 3 and sys.argv[1] == "--date":
        date_text = sys.argv[2]
        replace_date = None
        if len(sys.argv) >= 5 and sys.argv[3] == "--replace-date":
            replace_date = sys.argv[4]
        imported = collect_date(date_text)
        if replace_date and imported:
            delete_game_date(replace_date)
        return
    if len(sys.argv) >= 2:
        schedule_key = int(sys.argv[1])
    else:
        schedule_key = 506510
    game = parse_html(schedule_key)
    if DB_PATH.exists() and not Path(str(DB_PATH) + ".pre_period_migration.bak").exists():
        shutil.copy2(DB_PATH, str(DB_PATH) + ".pre_period_migration.bak")
    save_to_database(game)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
