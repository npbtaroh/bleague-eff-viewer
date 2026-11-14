#!/usr/bin/env python3
"""B.LEAGUE daily EFF rankings for a browser on this Mac."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import mimetypes
import importlib.util
import re
import sqlite3
import sys
from threading import Lock, Thread
import webbrowser
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_DB = Path.home() / "Documents" / "python" / "bleague_analytics" / "bleague_data.db"
STATIC_FILES = {
    "/": STATIC_DIR / "index.html",
    "/styles.css": STATIC_DIR / "styles.css",
    "/calculator.html": STATIC_DIR / "calculator.html",
    "/calculator.js": STATIC_DIR / "calculator.js",
    "/app.js": STATIC_DIR / "app.js",
}
ROLES = ("C", "PF", "SF", "SG", "PG")
LEAGUE_TABS = (("B.PREMIER", "1"), ("B.ONE", "2"), ("B.NEXT", "3"))
TODAY_SCHEDULE_CACHE: dict[str, object] = {"date": None, "loaded_at": 0.0, "games": []}
TODAY_SCHEDULE_LOCK = Lock()
TEAM_ALIAS_CACHE: dict[tuple[str, int], dict[tuple[str, str], str]] = {}


def get_parser_module():
    """Load the shared schedule helpers from the project-level parser."""
    parser_path = APP_DIR.parent / "parse_and_save.py"
    if not parser_path.is_file():
        raise FileNotFoundError(f"日程取得に必要なファイルがありません: {parser_path}")
    module_name = "bleague_schedule_parser"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(module_name, parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("試合日程の取得機能を読み込めませんでした")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def get_today_games() -> list[dict]:
    """Return cached today's schedule, serializing any background refresh."""
    with TODAY_SCHEDULE_LOCK:
        return _get_today_games_locked()


def _get_today_games_locked() -> list[dict]:
    """Discover today's scheduled clubs from the official league schedule."""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    today_text = today.isoformat()
    now = datetime.now().timestamp()
    if TODAY_SCHEDULE_CACHE["date"] == today_text and now - float(TODAY_SCHEDULE_CACHE["loaded_at"]) < 900:
        return list(TODAY_SCHEDULE_CACHE["games"])

    parser = get_parser_module()
    games_by_key: dict[str, dict] = {}

    def fetch_game_detail(key: str, league: str) -> dict:
        detail = parser.fetch_url(
            f"https://www.bleague.jp/game_detail/?ScheduleKey={key}&tab=1"
        ).decode("utf-8", errors="replace")
        detail_soup = parser.BeautifulSoup(detail, "html.parser")
        canonical = detail_soup.select_one('meta[property="og:url"]')
        canonical_key = re.search(r"ScheduleKey=(\d+)", canonical.get("content", "") if canonical else "")
        if not canonical_key or canonical_key.group(1) != key:
            raise RuntimeError(f"試合 {key} の公式詳細ページを照合できません")
        title = parser.text(detail_soup.title)
        actual_league = parser.league_from_title(title)
        if actual_league != league:
            raise RuntimeError(f"日程={league}、詳細ページ={actual_league} が一致しません")
        page_date = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})", title or "")
        if not page_date or (
            f"{int(page_date[1]):04d}-{int(page_date[2]):02d}-{int(page_date[3]):02d}" != today_text
        ):
            raise RuntimeError(f"試合 {key} の公式詳細ページの日付が本日と一致しません")
        home_nodes = detail_soup.select(".team-wrap.home .team-name .for-pc")
        away_nodes = detail_soup.select(".team-wrap.away .team-name .for-pc")
        home = parser.text(home_nodes[0]) if home_nodes else None
        away = parser.text(away_nodes[0]) if away_nodes else None
        if not home or not away:
            names = [parser.japanese_wovn(element) or parser.text(element)
                     for element in detail_soup.select(".team-wrap .team-name .for-pc")]
            names = [name for name in names if name]
            if len(names) >= 2:
                home, away = names[:2]
        if not home or not away:
            raise RuntimeError(f"試合 {key} のホーム・アウェイチームを取得できません")
        return {
            "schedule_key": int(key), "league": league,
            "home_team_name": home, "away_team_name": away,
        }

    for league, tab in LEAGUE_TABS:
        query = urlencode({
            "year": today.year, "mon": f"{today.month:02d}", "day": f"{today.day:02d}",
            "event": "", "club": "", "tab": tab, "ha": "", "fb": "",
        })
        schedule_html = parser.fetch_url(f"https://www.bleague.jp/schedule/?{query}").decode("utf-8", errors="replace")
        soup = parser.BeautifulSoup(schedule_html, "html.parser")
        visible_dates, current_dates = parser.schedule_page_dates(soup, today.year)
        if not visible_dates:
            raise RuntimeError(f"{league} の公式日程で日付表示を確認できません")
        if today_text not in visible_dates:
            continue
        if today_text not in current_dates:
            raise RuntimeError(f"{league} の日程選択日が本日と一致しません")

        keys: list[str] = []
        for link in soup.select('a[href*="ScheduleKey="]'):
            match = re.search(r"ScheduleKey=(\d+)", link.get("href", ""))
            if match and match.group(1) not in keys:
                keys.append(match.group(1))
        new_keys = []
        for key in keys:
            if key in games_by_key:
                if games_by_key[key]["league"] != league:
                    raise RuntimeError(f"ScheduleKey {key} が複数リーグの日程にあります")
                continue
            new_keys.append(key)
        # Fetch the per-game detail pages concurrently so the background warm-up
        # finishes before the user is likely to select today's view.
        with ThreadPoolExecutor(max_workers=4) as executor:
            details = executor.map(lambda key: fetch_game_detail(key, league), new_keys)
            for key, game in zip(new_keys, details):
                games_by_key[key] = game

    games = list(games_by_key.values())
    TODAY_SCHEDULE_CACHE.update({"date": today_text, "loaded_at": now, "games": games})
    return games


def connect_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"データベースが見つかりません: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def get_dates(path: Path) -> list[str]:
    with closing(connect_db(path)) as con:
        rows = con.execute("""
            SELECT DISTINCT g.game_date
            FROM games AS g JOIN player_period_stats AS p USING (schedule_key)
            WHERE p.period = 0 AND p.efficiency IS NOT NULL AND g.game_date IS NOT NULL
            ORDER BY g.game_date DESC
        """).fetchall()
    return [row[0] for row in rows]


def get_leagues(path: Path) -> list[str]:
    with closing(connect_db(path)) as con:
        rows = con.execute("""
            SELECT DISTINCT league FROM games
            WHERE league IS NOT NULL AND league <> ''
            ORDER BY CASE league
                WHEN 'B.PREMIER' THEN 1
                WHEN 'B.ONE' THEN 2
                WHEN 'B.NEXT' THEN 3
                ELSE 4
            END, league
        """).fetchall()
    return [row[0] for row in rows]


def current_season_year() -> int:
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    return today.year if today.month >= 7 else today.year - 1


def optimize_lineup(players: list[dict]) -> dict | None:
    """Maximize EFF over five distinct players assigned to eligible positions."""
    # Dynamic programming considers every legal role assignment exactly.
    # A bit in the mask indicates a filled position; each transition adds one player.
    states: dict[int, tuple[float, list[dict | None]]] = {0: (0.0, [None] * len(ROLES))}
    seen: set[str] = set()
    for player in players:  # SQL rows arrive in descending EFF order.
        player_key = str(player.get("player_id") or f"{player['schedule_key']}:{player['player_name']}")
        if player_key in seen:
            continue
        seen.add(player_key)
        eligible = set((player.get("position") or "").split("/"))
        next_states = dict(states)  # Skip this player, or assign them to one open role.
        for mask, (score, assignment) in states.items():
            for index, role in enumerate(ROLES):
                bit = 1 << index
                if role not in eligible or mask & bit:
                    continue
                new_mask = mask | bit
                new_score = score + float(player["efficiency"])
                previous = next_states.get(new_mask)
                if previous is None or new_score > previous[0]:
                    updated = assignment.copy()
                    updated[index] = player
                    next_states[new_mask] = (new_score, updated)
        states = next_states
    result = states.get((1 << len(ROLES)) - 1)
    if result is None:
        return None
    total, assignment = result
    return {
        "efficiency_total": round(total, 1),
        "players": [
            {"role": role, **assignment[index]}
            for index, role in enumerate(ROLES)
            if assignment[index] is not None
        ],
    }


SAME_TEAM_BONUS = {0: 0, 1: 0, 2: 1, 3: 3, 4: 5, 5: 10}


def optimize_score_lineup(players: list[dict]) -> dict | None:
    """Exactly maximize total EFF times the additive lineup bonus multiplier."""
    teams: dict[tuple[str, str], list[dict]] = {}
    seen: set[str] = set()
    for player in players:
        player_id = str(player["player_id"])
        if player_id in seen or not set((player.get("position") or "").split("/")) & set(ROLES):
            continue
        seen.add(player_id)
        if not player.get("team_name") or (player.get("matchup") or {}).get("result_mark") not in {"○", "×"}:
            return None  # A final score requires confirmed game results.
        teams.setdefault((player["league"], player["team_name"]), []).append(player)

    # Build each team's best distinct-player assignment for every position mask
    # and winner count, then combine disjoint masks across teams. Keeping the
    # largest team size preserves exactly the information the team bonus needs.
    states = {(0, 0, 0): (0.0, [None] * len(ROLES))}
    for team_players in teams.values():
        team_states = {(0, 0): (0.0, [None] * len(ROLES))}
        for player in team_players:
            updated = dict(team_states)
            won = int(player["matchup"]["result_mark"] == "○")
            eligible = (player.get("position") or "").split("/")
            for (mask, wins), (eff, assignment) in team_states.items():
                for index, role in enumerate(ROLES):
                    bit = 1 << index
                    if mask & bit or role not in eligible:
                        continue
                    key = (mask | bit, wins + won)
                    total = eff + float(player["efficiency"])
                    if key not in updated or total > updated[key][0]:
                        chosen = assignment.copy()
                        chosen[index] = player
                        updated[key] = (total, chosen)
            team_states = updated
        combined = dict(states)
        for (mask, wins, largest), (eff, assignment) in states.items():
            for (team_mask, team_wins), (team_eff, team_assignment) in team_states.items():
                if not team_mask or mask & team_mask:
                    continue
                key = (mask | team_mask, wins + team_wins, max(largest, team_mask.bit_count()))
                total = eff + team_eff
                if key not in combined or total > combined[key][0]:
                    chosen = [team_assignment[i] or assignment[i] for i in range(len(ROLES))]
                    combined[key] = (total, chosen)
        states = combined
    full_mask = (1 << len(ROLES)) - 1
    candidates = []
    for (mask, wins, largest), (eff, assignment) in states.items():
        if mask == full_mask:
            percent = wins * 10 + SAME_TEAM_BONUS[largest]
            candidates.append((eff * (100 + percent) / 100, eff, wins, largest, assignment))
    if not candidates:
        return None
    score, eff, wins, largest, assignment = max(candidates, key=lambda item: (item[0], item[1]))
    return {
        "score_total": round(score, 2),
        "efficiency_total": round(eff, 1),
        "winning_players": wins,
        "win_bonus_percent": wins * 10,
        "same_team_bonus_percent": SAME_TEAM_BONUS[largest],
        "largest_team_count": largest,
        "bonus_percent": wins * 10 + SAME_TEAM_BONUS[largest],
        "players": [{**assignment[i], "role": role} for i, role in enumerate(ROLES)],
    }


def get_official_team_aliases(path: Path, season_year: int) -> dict[tuple[str, str], str]:
    """Map full game names to the official short names used in season statistics."""
    cache_key = (str(path), season_year)
    if cache_key in TEAM_ALIAS_CACHE:
        return TEAM_ALIAS_CACHE[cache_key]
    with closing(connect_db(path)) as con:
        rows = con.execute("""
            WITH latest AS (
                SELECT g.league,p.player_id,p.team_name,g.game_date,g.schedule_key,
                       ROW_NUMBER() OVER (
                           PARTITION BY g.league,p.player_id
                           ORDER BY g.game_date DESC,g.schedule_key DESC
                       ) AS row_num
                FROM player_period_stats p JOIN games g USING(schedule_key)
                WHERE p.period=0 AND COALESCE(p.minutes_seconds,0)>0 AND g.game_date>=?
            )
            SELECT l.league,l.team_name AS full_name,o.team_name AS short_name,
                   COUNT(DISTINCT l.player_id) AS player_count
            FROM latest l JOIN official_player_season_stats o
              ON o.player_id=l.player_id AND o.league=l.league AND o.season_year=?
            WHERE l.row_num=1
            GROUP BY l.league,l.team_name,o.team_name
            ORDER BY l.league,l.team_name,player_count DESC,o.team_name
        """, (f"{season_year}-07-01", season_year)).fetchall()
    aliases: dict[tuple[str, str], str] = {}
    for league, full_name, short_name, _count in rows:
        aliases.setdefault((league, full_name), short_name)
    TEAM_ALIAS_CACHE[cache_key] = aliases
    return aliases


def short_team_name(aliases: dict[tuple[str, str], str], league: str | None, name: str | None) -> str | None:
    if not name or not league:
        return name
    return aliases.get((league, name), name)


def matchup_for_team(contexts: dict[tuple[str, str], dict], league: str | None,
                     team_name: str | None, aliases: dict[tuple[str, str], str]) -> tuple[str | None, dict | None]:
    """Resolve context whether its input team name is full or officially abbreviated."""
    if not team_name:
        return league, None
    if league:
        context = contexts.get((league, team_name))
        if context is not None:
            return league, context
        full_names = [full for (item_league, full), short in aliases.items()
                      if item_league == league and short == team_name]
        if len(full_names) == 1:
            return league, contexts.get((league, full_names[0]))
    matches = [(item_league, context) for (item_league, full), context in contexts.items()
               if full == team_name]
    if not matches:
        matches = [(item_league, contexts.get((item_league, full)))
                   for (item_league, full), short in aliases.items()
                   if short == team_name and (item_league, full) in contexts]
    if len(matches) == 1:
        return matches[0]
    return league, None


def display_matchup(context: dict | None, league: str | None,
                    aliases: dict[tuple[str, str], str]) -> dict | None:
    if not context:
        return None
    result = dict(context)
    result["opponent_name"] = short_team_name(aliases, league, result.get("opponent_name"))
    return result


def build_matchup_context(path: Path, game_date: str, league: str,
                          scheduled_games: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """Build compact team/opponent records and the selected day's result."""
    season_year = current_season_year()
    season_start = f"{season_year}-07-01"
    with closing(connect_db(path)) as con:
        if scheduled_games is None:
            where_league = " AND league=?" if league != "all" else ""
            games = [dict(zip(("league", "home_team_name", "away_team_name", "home_score", "away_score"), row))
                     for row in con.execute(
                         "SELECT league,home_team_name,away_team_name,home_score,away_score "
                         "FROM games WHERE game_date=?" + where_league,
                         [game_date] + ([league] if league != "all" else []),
                     ).fetchall()]
        else:
            games = [dict(game) for game in scheduled_games
                     if league == "all" or game["league"] == league]
            keys = [game.get("schedule_key") for game in games if game.get("schedule_key") is not None]
            if keys:
                stored_scores = {row[0]: (row[1], row[2]) for row in con.execute(
                    "SELECT schedule_key,home_score,away_score FROM games WHERE schedule_key IN (" +
                    ",".join("?" for _ in keys) + ")", keys
                ).fetchall()}
                for game in games:
                    scores = stored_scores.get(game.get("schedule_key"))
                    if scores:
                        game["home_score"], game["away_score"] = scores
        record_rows = con.execute("""
            SELECT league,team_name,SUM(wins) AS wins,SUM(losses) AS losses
            FROM (
                SELECT league,home_team_name AS team_name,
                       CASE WHEN home_score>away_score THEN 1 ELSE 0 END AS wins,
                       CASE WHEN home_score<away_score THEN 1 ELSE 0 END AS losses
                FROM games WHERE game_date>=? AND game_date<?
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                UNION ALL
                SELECT league,away_team_name AS team_name,
                       CASE WHEN away_score>home_score THEN 1 ELSE 0 END AS wins,
                       CASE WHEN away_score<home_score THEN 1 ELSE 0 END AS losses
                FROM games WHERE game_date>=? AND game_date<?
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
            ) GROUP BY league,team_name
        """, [season_start, game_date] * 2).fetchall()
    records = {(row[0], row[1]): (int(row[2] or 0), int(row[3] or 0)) for row in record_rows}
    contexts: dict[tuple[str, str], dict] = {}
    matchups = []
    for game in games:
        game_league = game["league"]
        home, away = game["home_team_name"], game["away_team_name"]
        home_record = records.get((game_league, home), (0, 0))
        away_record = records.get((game_league, away), (0, 0))
        home_score, away_score = game.get("home_score"), game.get("away_score")
        home_result = "○" if home_score is not None and away_score is not None and home_score > away_score else (
            "×" if home_score is not None and away_score is not None and home_score < away_score else None)
        away_result = "○" if home_score is not None and away_score is not None and away_score > home_score else (
            "×" if home_score is not None and away_score is not None and away_score < home_score else None)
        home_wins = home_record[0] + (1 if home_result == "○" else 0)
        home_losses = home_record[1] + (1 if home_result == "×" else 0)
        away_wins = away_record[0] + (1 if away_result == "○" else 0)
        away_losses = away_record[1] + (1 if away_result == "×" else 0)
        contexts[(game_league, home)] = {
            "team_wins": home_wins, "team_losses": home_losses,
            "opponent_name": away, "opponent_wins": away_record[0], "opponent_losses": away_record[1],
            "team_score": home_score, "opponent_score": away_score, "result_mark": home_result,
        }
        contexts[(game_league, away)] = {
            "team_wins": away_wins, "team_losses": away_losses,
            "opponent_name": home, "opponent_wins": home_record[0], "opponent_losses": home_record[1],
            "team_score": away_score, "opponent_score": home_score, "result_mark": away_result,
        }
        matchups.append({
            "league": game_league, "home_team_name": home, "away_team_name": away,
            "home_wins": home_wins, "home_losses": home_losses,
            "away_wins": away_wins, "away_losses": away_losses,
            "home_score": home_score, "away_score": away_score,
            "home_result": home_result, "away_result": away_result,
        })
    return contexts, matchups


def ensure_prediction_schema(path: Path) -> None:
    """Add the small, non-destructive table used to freeze daily predictions."""
    with closing(sqlite3.connect(path, timeout=10)) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS daily_lineup_predictions (
            game_date TEXT NOT NULL,
            league_scope TEXT NOT NULL,
            player_league TEXT,
            role TEXT NOT NULL,
            player_id TEXT NOT NULL,
            player_name TEXT,
            team_name TEXT,
            position TEXT,
            predicted_efficiency REAL NOT NULL,
            games_played INTEGER NOT NULL,
            lineup_efficiency_total REAL NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(game_date, league_scope, role)
        )""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(daily_lineup_predictions)")}
        if "player_league" not in columns:
            con.execute("ALTER TABLE daily_lineup_predictions ADD COLUMN player_league TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_lineup_player ON daily_lineup_predictions(player_id, game_date)")
        con.commit()


def historical_prediction_candidates(path: Path, game_date: str, league: str,
                                    participating_teams: set[tuple[str, str]] | None = None) -> list[dict]:
    """Rebuild pregame season averages from games strictly before the selected date."""
    season_year = current_season_year()
    season_start = f"{season_year}-07-01"
    league_sql = " AND g.league = ?" if league != "all" else ""
    params: list[object] = [season_start, game_date]
    if league != "all":
        params.append(league)
    with closing(connect_db(path)) as con:
        if participating_teams is None:
            scheduled = con.execute(
                "SELECT league,home_team_name,away_team_name FROM games AS g WHERE game_date=?" + league_sql,
                [game_date] + ([league] if league != "all" else []),
            ).fetchall()
            participating = {(row[0], row[1]) for row in scheduled} | {(row[0], row[2]) for row in scheduled}
        else:
            participating = participating_teams
            if league != "all":
                participating = {team for team in participating if team[0] == league}
        if not participating:
            return []
        sql = """
            SELECT g.game_date,g.schedule_key,g.league,p.player_id,p.player_name,p.team_name,p.position,
                   p.minutes_seconds,p.points,p.fgm,p.fga,p.ftm,p.fta,p.total_rebounds,p.assists,
                   p.turnovers,p.steals,p.blocks,p.blocks_received,p.fouls,p.fouls_drawn
            FROM player_period_stats p JOIN games g USING(schedule_key)
            WHERE p.period=0 AND COALESCE(p.minutes_seconds,0)>0
              AND g.game_date>=? AND g.game_date<?
        """ + league_sql + " ORDER BY p.player_id,g.league,g.game_date,g.schedule_key"
        rows = con.execute(sql, params).fetchall()

    candidates: dict[tuple[str, str], dict] = {}
    for row in rows:
        (row_date,row_key,row_league,player_id,player_name,team_name,position,minutes,
         points,fgm,fga,ftm,fta,rebounds,assists,turnovers,steals,blocks,
         blocks_received,fouls,fouls_drawn) = row
        key = (str(player_id), row_league)
        item = candidates.setdefault(key, {
            "player_id": str(player_id), "player_name": player_name, "league": row_league,
            "team_name": team_name, "position": position, "games_played": 0,
            "points": 0, "fgm": 0, "fga": 0, "ftm": 0, "fta": 0,
            "total_rebounds": 0, "assists": 0, "turnovers": 0, "steals": 0,
            "blocks": 0, "blocks_received": 0, "fouls": 0, "fouls_drawn": 0,
            "last_game": (row_date, row_key),
        })
        item["games_played"] += 1
        for field, value in (("points",points),("fgm",fgm),("fga",fga),("ftm",ftm),("fta",fta),
                             ("total_rebounds",rebounds),("assists",assists),("turnovers",turnovers),
                             ("steals",steals),("blocks",blocks),("blocks_received",blocks_received),
                             ("fouls",fouls),("fouls_drawn",fouls_drawn)):
            item[field] += int(value or 0)
        if (row_date, row_key) >= item["last_game"]:
            item["team_name"] = team_name
            item["position"] = position or item["position"]
            item["player_name"] = player_name or item["player_name"]
            item["last_game"] = (row_date, row_key)

    output = []
    for item in candidates.values():
        if (item["league"], item["team_name"]) not in participating:
            continue
        gp = item["games_played"]
        if not gp:
            continue
        total_eff = (
            item["points"] + item["assists"] + item["blocks"] + item["steals"]
            + item["fouls_drawn"] + item["total_rebounds"] - item["turnovers"]
            - item["blocks_received"] - item["fouls"] - (item["fga"] - item["fgm"])
            - (item["fta"] - item["ftm"])
        )
        item["efficiency"] = round(total_eff / gp, 1)
        output.append(item)
    output.sort(key=lambda player: (-player["efficiency"], player["player_name"] or ""))
    return output


def get_or_create_daily_prediction(path: Path, game_date: str, league: str,
                                   candidate_players: list[dict] | None = None,
                                   matchup_contexts: dict | None = None,
                                   team_aliases: dict[tuple[str, str], str] | None = None) -> dict | None:
    """Return the frozen prediction for a date, creating it once if needed."""
    ensure_prediction_schema(path)
    with closing(sqlite3.connect(path, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        saved = con.execute("""SELECT role,player_id,player_name,team_name,position,player_league,
                predicted_efficiency,games_played,lineup_efficiency_total
            FROM daily_lineup_predictions WHERE game_date=? AND league_scope=? ORDER BY
            CASE role WHEN 'C' THEN 1 WHEN 'PF' THEN 2 WHEN 'SF' THEN 3 WHEN 'SG' THEN 4 WHEN 'PG' THEN 5 ELSE 6 END""",
            (game_date, league)).fetchall()
    if saved:
        lineup = {
            "efficiency_total": saved[0]["lineup_efficiency_total"],
            "players": [{"role": row["role"], "player_id": row["player_id"],
                         "player_name": row["player_name"], "team_name": row["team_name"],
                         "position": row["position"], "efficiency": row["predicted_efficiency"],
                         "league": row["player_league"],
                         "games_played": row["games_played"]} for row in saved],
        }
    else:
        candidates = candidate_players if candidate_players is not None else historical_prediction_candidates(path, game_date, league)
        lineup = optimize_lineup(candidates)
        if lineup is None:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        rows = [{
            "game_date": game_date, "league_scope": league, "player_league": player.get("league"),
            "role": player["role"],
            "player_id": str(player["player_id"]), "player_name": player["player_name"],
            "team_name": player["team_name"], "position": player["position"],
            "predicted_efficiency": player["efficiency"],
            "games_played": player.get("games_played", 0),
            "lineup_efficiency_total": lineup["efficiency_total"], "created_at": now,
        } for player in lineup["players"]]
        with closing(sqlite3.connect(path, timeout=10)) as con:
            con.executemany("""INSERT OR IGNORE INTO daily_lineup_predictions
                (game_date,league_scope,player_league,role,player_id,player_name,team_name,position,
                 predicted_efficiency,games_played,lineup_efficiency_total,created_at)
                VALUES (:game_date,:league_scope,:player_league,:role,:player_id,:player_name,:team_name,:position,
                        :predicted_efficiency,:games_played,:lineup_efficiency_total,:created_at)""", rows)
            con.commit()
        return get_or_create_daily_prediction(path, game_date, league,
                                              matchup_contexts=matchup_contexts,
                                              team_aliases=team_aliases)

    if lineup:
        with closing(connect_db(path)) as con:
            actual_rows = con.execute("""SELECT p.player_id,p.efficiency,p.minutes_seconds
                FROM player_period_stats p JOIN games g USING(schedule_key)
                WHERE p.period=0 AND g.game_date=?""", (game_date,)).fetchall()
        actual_by_id = {str(row[0]): row for row in actual_rows}
        if matchup_contexts is None:
            matchup_contexts, _ = build_matchup_context(path, game_date, league)
        if team_aliases is None:
            team_aliases = get_official_team_aliases(path, current_season_year())
        actual_total = 0
        played_count = 0
        for player in lineup["players"]:
            player_league, context = matchup_for_team(
                matchup_contexts, player.get("league"), player.get("team_name"), team_aliases
            )
            if player.get("league") is None:
                player["league"] = player_league
            player["matchup"] = context
            if context:
                context = dict(context)
                context["opponent_name"] = short_team_name(
                    team_aliases, player.get("league"), context.get("opponent_name")
                )
                player["matchup"] = context
            player["team_name"] = short_team_name(
                team_aliases, player.get("league"), player.get("team_name")
            )
            actual = actual_by_id.get(str(player["player_id"]))
            if actual and (actual[2] or 0) > 0:
                player["actual_efficiency"] = actual[1]
                actual_total += int(actual[1] or 0)
                played_count += 1
            else:
                player["actual_efficiency"] = None
        lineup["actual_efficiency_total"] = actual_total if played_count else None
        lineup["actual_players_played"] = played_count
    return lineup


def get_rankings(path: Path, game_date: str, league: str) -> dict:
    today_mode = game_date == "today"
    season_mode = game_date in {"season", "today"}
    season_year = current_season_year()
    team_aliases = get_official_team_aliases(path, season_year)
    league_clause = ""
    params: list[str] = [] if season_mode else [game_date]
    if league != "all":
        league_clause = " AND o.league = ?" if season_mode else " AND g.league = ?"
        params.append(league)
    selected_today_games: list[dict] = []
    today_player_ids: list[str] = []
    today_team_pairs: list[tuple[str, str]] = []
    today_matchups: list[dict] = []
    matchup_contexts: dict[tuple[str, str], dict] = {}
    season_updated_at = None
    if today_mode:
        selected_today_games = [game for game in get_today_games()
                                if league == "all" or game["league"] == league]
        today_teams = sorted({
            name for game in selected_today_games
            for name in (game["home_team_name"], game["away_team_name"])
        })
        if not today_teams:
            raise ValueError("本日は公式日程に試合がありません。")
        today_team_pairs = sorted({
            (game["league"], short_team_name(
                team_aliases, game["league"], team
            ) or team)
            for game in selected_today_games
            for team in (game["home_team_name"], game["away_team_name"])
        })
        with closing(connect_db(path)) as con:
            team_rows = con.execute(
                "SELECT DISTINCT p.player_id FROM player_period_stats p JOIN games g USING(schedule_key) "
                "WHERE p.period=0 AND p.minutes_seconds>0 AND g.game_date>=? "
                "AND g.game_date<? AND p.team_name IN (" + ",".join("?" for _ in today_teams) + ")",
                [f"{season_year}-07-01", f"{season_year + 1}-07-01", *today_teams],
            ).fetchall()
            today_text = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
        matchup_contexts, today_matchups = build_matchup_context(
            path, today_text, league, selected_today_games
        )
        for matchup in today_matchups:
            matchup["home_team_name"] = short_team_name(
                team_aliases, matchup["league"], matchup["home_team_name"]
            )
            matchup["away_team_name"] = short_team_name(
                team_aliases, matchup["league"], matchup["away_team_name"]
            )
        today_player_ids = [str(row[0]) for row in team_rows]
        if not today_player_ids:
            raise ValueError("本日の対戦チームに、今季スタッツがある選手が見つかりません。")
    with closing(connect_db(path)) as con:
        if season_mode:
            player_filter = ""
            season_params = list(params)
            if today_mode:
                player_filter = " AND (" + " OR ".join(
                    "(o.league=? AND o.team_name=?)" for _ in today_team_pairs
                ) + ")"
                season_params.extend(value for pair in today_team_pairs for value in pair)
            rows = con.execute(f"""
                SELECT o.season_year, o.league, NULL AS schedule_key, o.player_id, o.team_name,
                       o.player_name, o.position,
                       1.0 * o.efficiency / o.games_played,
                       1.0 * o.points / o.games_played,
                       1.0 * o.total_rebounds / o.games_played,
                       1.0 * o.assists / o.games_played,
                       1.0 * o.minutes_seconds / o.games_played,
                       o.games_played
                FROM official_player_season_stats AS o
                WHERE o.season_year = ?{league_clause}{player_filter}
                  AND o.games_played > 0
                ORDER BY 8 DESC, player_name COLLATE NOCASE, team_name
            """, [season_year] + season_params).fetchall()
            if not rows:
                raise RuntimeError("公式シーズン集計がまだありません。データ更新を実行してください。")
            if today_mode:
                game_count = len(selected_today_games)
            else:
                game_count_league_clause = " AND g.league = ?" if league != "all" else ""
                game_count = con.execute(f"""
                    SELECT COUNT(*) FROM games AS g
                    WHERE g.game_date >= ? AND g.game_date < ?{game_count_league_clause}
                """, [f"{season_year}-07-01", f"{season_year + 1}-07-01"]
                     + ([league] if league != "all" else [])).fetchone()[0]
            season_updated_at = con.execute(
                "SELECT MAX(updated_at) FROM official_player_season_stats WHERE season_year=?",
                [season_year],
            ).fetchone()[0]
        else:
            rows = con.execute(f"""
                SELECT g.game_date, g.league, g.schedule_key,
                       p.player_id, p.team_name, p.player_name, p.position,
                       p.efficiency, p.points, p.total_rebounds, p.assists,
                       p.minutes_seconds,
                       CASE WHEN COALESCE(p.minutes_seconds, 0) > 0 THEN 1 ELSE 0 END
                FROM player_period_stats AS p
                JOIN games AS g USING (schedule_key)
                WHERE p.period = 0 AND p.efficiency IS NOT NULL
                  AND g.game_date = ?{league_clause}
                ORDER BY p.efficiency DESC, p.player_name COLLATE NOCASE, g.schedule_key
            """, params).fetchall()
            game_count_league_clause = " AND g.league = ?" if league != "all" else ""
            game_count = con.execute(f"""
                SELECT COUNT(*) FROM games AS g
                WHERE g.game_date = ?{game_count_league_clause}
            """, params).fetchone()[0]
    players = []
    for row in rows:
        (row_date, row_league, schedule_key, player_id, team_name, player_name,
         position, efficiency, points, rebounds, assists, seconds, appearances) = row
        minutes = f"{int(seconds or 0) // 60}:{int(seconds or 0) % 60:02d}"
        resolved_league, raw_matchup = matchup_for_team(
            matchup_contexts, row_league, team_name, team_aliases
        )
        players.append({
            "game_date": row_date,
            "league": row_league,
            "schedule_key": schedule_key,
            "player_id": player_id,
            "team_name": short_team_name(team_aliases, row_league, team_name),
            "player_name": player_name,
            "position": position,
            "efficiency": round(float(efficiency or 0), 1) if season_mode else efficiency,
            "points": round(float(points or 0), 1) if season_mode else points,
            "total_rebounds": round(float(rebounds or 0), 1) if season_mode else rebounds,
            "assists": round(float(assists or 0), 1) if season_mode else assists,
            "minutes": minutes,
            "games_played": appearances,
            "matchup": display_matchup(raw_matchup, resolved_league, team_aliases),
        })
    prediction_lineup = None
    if today_mode:
        today_text = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
        today_participating = {
            (game["league"], team)
            for game in selected_today_games
            for team in (game["home_team_name"], game["away_team_name"])
        }
        pregame_players = historical_prediction_candidates(
            path, today_text, league, today_participating
        )
        prediction_lineup = get_or_create_daily_prediction(
            path, today_text, league, pregame_players, matchup_contexts, team_aliases
        )
    elif not season_mode:
        matchup_contexts, _ = build_matchup_context(path, game_date, league)
        for player in players:
            player_league, raw_matchup = matchup_for_team(
                matchup_contexts, player["league"], player["team_name"], team_aliases
            )
            player["matchup"] = display_matchup(raw_matchup, player_league, team_aliases)
        prediction_lineup = get_or_create_daily_prediction(
            path, game_date, league, matchup_contexts=matchup_contexts,
            team_aliases=team_aliases
        )
    by_position = {role: [] for role in ROLES}
    for player in players:
        for role in ROLES:
            if role in (player["position"] or "").split("/"):
                by_position[role].append(player)
    return {
        "date": game_date,
        "scope": "today" if today_mode else ("season" if season_mode else "daily"),
        "league": league,
        "season_year": season_year,
        "game_count": game_count,
        "overall": players[:10],
        "best_lineup": optimize_lineup(players),
        "prediction_lineup": prediction_lineup,
        "score_lineup": optimize_score_lineup(players) if not season_mode else None,
        "matchups": today_matchups,
        "by_position": {role: by_position[role][:5] for role in ROLES},
        "season_updated_at": season_updated_at if season_mode else None,
    }


def get_reconciliation(path: Path, league: str) -> dict:
    with closing(connect_db(path)) as con:
        con.row_factory = sqlite3.Row
        exists = con.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='season_eff_reconciliation'""").fetchone()
        if not exists:
            return {"ready": False, "differences": []}
        season_year = current_season_year()
        where = "season_year=?"
        params: list[object] = [season_year]
        if league != "all":
            where += " AND league=?"
            params.append(league)
        rows = con.execute(f"""SELECT league,player_id,player_name,team_name,
            official_efficiency,game_efficiency,difference,checked_at
            FROM season_eff_reconciliation WHERE {where}
            ORDER BY league,ABS(difference) DESC,player_name""", params).fetchall()
        checked = con.execute("""SELECT MAX(updated_at) FROM official_player_season_stats
            WHERE season_year=?""", [season_year]).fetchone()[0]
    return {
        "ready": checked is not None,
        "checked_at": checked,
        "differences": [dict(row) for row in rows],
    }


def handler_for(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BLeagueEFF"

        def log_message(self, format: str, *args) -> None:
            return

        def respond(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def respond_json(self, data: object, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.respond(body, "application/json; charset=utf-8", status)

        def do_GET(self) -> None:
            request = urlparse(self.path)
            if request.path == "/api/dates":
                try:
                    self.respond_json({"dates": get_dates(db_path), "leagues": get_leagues(db_path)})
                except (sqlite3.Error, OSError) as error:
                    self.respond_json({"error": str(error)}, 500)
                return
            if request.path == "/api/rankings":
                query = parse_qs(request.query)
                game_date = query.get("date", [""])[0]
                league = query.get("league", ["all"])[0]
                try:
                    if game_date not in {"season", "today"} and game_date not in get_dates(db_path):
                        self.respond_json({"error": "選択した日付のデータがありません。"}, 400)
                        return
                    if league != "all" and league not in get_leagues(db_path):
                        self.respond_json({"error": "選択したリーグが見つかりません。"}, 400)
                        return
                    self.respond_json(get_rankings(db_path, game_date, league))
                except ValueError as error:
                    self.respond_json({"error": str(error)}, 400)
                except (sqlite3.Error, OSError, RuntimeError) as error:
                    self.respond_json({"error": str(error)}, 502)
                return
            if request.path == "/api/reconciliation":
                league = parse_qs(request.query).get("league", ["all"])[0]
                try:
                    self.respond_json(get_reconciliation(db_path, league))
                except (sqlite3.Error, OSError) as error:
                    self.respond_json({"error": str(error)}, 500)
                return
            file_path = STATIC_FILES.get(request.path)
            if file_path is None or not file_path.is_file():
                self.respond(b"Not found", "text/plain; charset=utf-8", 404)
                return
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            charset = "; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""
            self.respond(file_path.read_bytes(), content_type + charset)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="B.LEAGUEの日別EFFランキング")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="bleague_data.db の場所")
    parser.add_argument("--port", type=int, default=0, help="既定では空きポートを自動選択")
    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()
    if not get_dates(db_path):
        raise SystemExit("EFFデータが見つかりません。データベースを確認してください。")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(db_path))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"B.LEAGUE EFFランキング: {url}")
    print("終了するには、このターミナルで Control-C を押してください。")
    Thread(target=prefetch_today_schedule, args=(db_path,), name="today-schedule-prefetch", daemon=True).start()
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nアプリを終了しました。")
    finally:
        server.server_close()


def prefetch_today_schedule(db_path: Path) -> None:
    """Warm today's schedule and freeze its pregame lineup in the background."""
    try:
        games = get_today_games()
        print(f"本日の試合日程を準備しました（{len(games)}試合）")
        if games:
            get_rankings(db_path, "today", "all")
            print("本日の試合前予想5人を保存しました")
    except Exception as error:
        print(f"本日の日程または試合前予想を事前準備できませんでした。選択時に再試行します: {error}")


if __name__ == "__main__":
    main()
