#!/usr/bin/env python3
"""Export the local ranking app's API responses as GitHub Pages JSON files."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def load_app():
    path = ROOT / "eff_app" / "app.py"
    spec = importlib.util.spec_from_file_location("bleague_eff_app_static_export", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"アプリを読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Pages向けの静的データを書き出します")
    parser.add_argument("--db", type=Path, default=ROOT / "bleague_data.db")
    parser.add_argument("--out", type=Path, default=ROOT / "site")
    args = parser.parse_args()
    db_path = args.db.resolve()
    out_dir = args.out.resolve()
    if not db_path.is_file():
        parser.error(f"SQLiteデータベースがありません: {db_path}")

    app = load_app()
    out_dir.mkdir(parents=True, exist_ok=True)
    static_dir = ROOT / "eff_app" / "static"
    for asset in ("index.html", "app.js", "styles.css", "calculator.html", "calculator.js"):
        shutil.copy2(static_dir / asset, out_dir / asset)

    dates = app.get_dates(db_path)
    leagues = app.get_leagues(db_path)
    generated_at = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes")
    data_dir = out_dir / "data"
    rankings_dir = data_dir / "rankings"
    rankings_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "dates.json").open("w", encoding="utf-8") as stream:
        json.dump({"dates": dates, "leagues": leagues, "generated_at": generated_at},
                  stream, ensure_ascii=False, indent=2)

    scopes = ["today", "season", *dates]
    all_leagues = ["all", *leagues]
    errors = []
    for scope in scopes:
        for league in all_leagues:
            file_name = f"{scope}__{league}.json"
            try:
                payload = app.get_rankings(db_path, scope, league)
                payload["generated_at"] = generated_at
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                payload = {"error": str(exc), "scope": scope, "league": league,
                           "generated_at": generated_at}
                errors.append(f"{scope} / {league}: {exc}")
            with (rankings_dir / file_name).open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)

    print(f"静的ページを生成しました: {out_dir}")
    print(f"試合日 {len(dates)}日、ランキングJSON {len(scopes) * len(all_leagues)}件")
    if errors:
        print(f"注意: 利用できない条件が {len(errors)}件あります（本日の日程なし等）")
        for error in errors:
            print(f"  {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
