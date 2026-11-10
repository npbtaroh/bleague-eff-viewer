const dateSelect = document.querySelector("#date-select");
const leagueSelect = document.querySelector("#league-select");
const statusBox = document.querySelector("#status");
const results = document.querySelector("#results");
const overallBody = document.querySelector("#overall-body");
const overallEmpty = document.querySelector("#overall-empty");
const positionGrid = document.querySelector("#position-grid");
const lineupGrid = document.querySelector("#lineup-grid");
const lineupTotal = document.querySelector("#lineup-total");
const lineupHeading = document.querySelector("#lineup-heading");
const lineupDescription = document.querySelector("#lineup-description");
const lineupEmpty = document.querySelector("#lineup-empty");
const selectedCaption = document.querySelector("#selected-caption");
const gameCount = document.querySelector("#game-count");
const seasonAverageNote = document.querySelector("#season-average-note");
const officialUpdatedAt = document.querySelector("#official-updated-at");
const dataUpdatedAt = document.querySelector("#data-updated-at");
const todayScopeNote = document.querySelector("#today-scope-note");
const matchupsBlock = document.querySelector("#matchups-block");
const matchupsGrid = document.querySelector("#matchups-grid");
const predictionBlock = document.querySelector("#prediction-block");
const predictionGrid = document.querySelector("#prediction-grid");
const predictionTotal = document.querySelector("#prediction-total");
const predictionEmpty = document.querySelector("#prediction-empty");
const predictionDescription = document.querySelector("#prediction-description");
const teamContextHeading = document.querySelector("#team-context-heading");
const gamesPlayedHeading = document.querySelector("#games-played-heading");
const officialSeasonLink = document.querySelector("#official-season-link");
const ROLE_ORDER = ["C", "PF", "SF", "SG", "PG"];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "データを読み込めませんでした。");
  if (payload.error) throw new Error(payload.error);
  return payload;
}

function formatDate(date) {
  const [year, month, day] = date.split("-").map(Number);
  return `${year}年${month}月${day}日`;
}

function formatMetric(value) {
  const number = Number(value);
  return Number.isInteger(number) ? String(number) : number.toFixed(1);
}

function formatUpdatedAt(value) {
  if (!value) return "未取得";
  const date = new Date(value.replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ja-JP");
}

function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.append(option);
}

function addTeamContext(parent, teamName, league, matchup, compactRanking = false) {
  if (compactRanking) {
    const context = element("div", "ranking-team-context");
    const teamLine = element("div", "ranking-team-line");
    teamLine.append(element("strong", "ranking-team-name", teamName || "—"));
    if (matchup) {
      const record = element("small", "ranking-team-record");
      if (matchup.result_mark) {
        record.append(element("strong", `team-result-mark ${matchup.result_mark === "○" ? "win" : "loss"}`, matchup.result_mark));
        record.append(document.createTextNode(" "));
      }
      record.append(document.createTextNode(`${matchup.team_wins}勝${matchup.team_losses}敗`));
      teamLine.append(record);
    }
    context.append(teamLine);
    if (matchup?.opponent_name) {
      const score = matchup.team_score !== null && matchup.team_score !== undefined
        && matchup.opponent_score !== null && matchup.opponent_score !== undefined
        ? `${matchup.team_score}–${matchup.opponent_score}`
        : `${matchup.opponent_wins}勝${matchup.opponent_losses}敗`;
      const matchupLine = element("div", "ranking-matchup-line");
      matchupLine.append(element("small", "ranking-opponent", `vs ${matchup.opponent_name}`));
      matchupLine.append(element("small", "ranking-outcome", score));
      context.append(matchupLine);
    }
    parent.append(context);
    return;
  }

  const context = element("div", "team-context");
  context.append(element("strong", "team-context-name", teamName || "—"));
  if (!matchup) {
    parent.append(context);
    return;
  }
  const record = element("small", "team-context-record");
  if (matchup.result_mark) {
    record.append(element("strong", `team-result-mark ${matchup.result_mark === "○" ? "win" : "loss"}`, matchup.result_mark));
    record.append(document.createTextNode(" "));
  }
  record.append(document.createTextNode(`${matchup.team_wins}勝${matchup.team_losses}敗`));
  context.append(record);
  if (matchup.opponent_name) {
    const score = matchup.team_score !== null && matchup.team_score !== undefined
      && matchup.opponent_score !== null && matchup.opponent_score !== undefined
      ? `${matchup.team_score}–${matchup.opponent_score}`
      : `${matchup.opponent_wins}勝${matchup.opponent_losses}敗`;
    const matchupLine = element("div", "team-context-matchup");
    matchupLine.append(element("small", "team-context-opponent", `vs ${matchup.opponent_name}`));
    matchupLine.append(element("small", "team-context-outcome", score));
    context.append(matchupLine);
  }
  parent.append(context);
}

function addOverallRow(player, index) {
  const row = document.createElement("tr");
  const values = [
    [String(index + 1), ""],
    [player.player_name || "名前不明", "player-cell"],
    [player.position || "—", "mobile-detail"],
    [formatMetric(player.efficiency), "numeric eff-value"],
    [player.points === null ? "—" : formatMetric(player.points), "numeric"],
    [player.total_rebounds === null ? "—" : formatMetric(player.total_rebounds), "numeric"],
    [player.assists === null ? "—" : formatMetric(player.assists), "numeric"],
    [player.minutes || "—", "numeric"],
    [String(player.games_played ?? 0), "numeric mobile-hide games-played-column"],
  ];
  for (let index = 0; index < values.length; index += 1) {
    if (index === 2) {
      const cell = element("td", "team-cell");
      addTeamContext(cell, player.team_name, player.league, player.matchup, true);
      row.append(cell);
    }
    const [value, className] = values[index];
    const cell = element("td", className, value);
    if (index === 1) {
      cell.replaceChildren(
        element("strong", "", value),
        element("small", "player-mobile-meta", player.position || "—"),
      );
    }
    row.append(cell);
  }
  return row;
}

function addPositionCard(role, players) {
  const card = element("section", "position-card");
  const heading = element("h3", "", role);
  heading.append(element("span", "", "TOP 5"));
  card.append(heading);
  const list = element("ol", "position-list");
  if (!players.length) {
    const item = element("li", "position-row", "該当選手なし");
    item.style.gridTemplateColumns = "1fr";
    list.append(item);
  } else {
    players.forEach((player, index) => {
      const item = element("li", "position-row");
      item.append(element("span", "position-rank", String(index + 1)));
      const details = element("div", "position-player");
      details.append(element("strong", "", player.player_name || "名前不明"));
      addTeamContext(details, player.team_name, player.league, player.matchup, true);
      item.append(details, element("span", "position-eff", formatMetric(player.efficiency)));
      list.append(item);
    });
  }
  card.append(list);
  return card;
}

function addLineupSlot(player, effLabel = "EFF") {
  const slot = element("div", "lineup-slot");
  slot.append(element("div", "lineup-role", `配置: ${player.role}`));
  slot.append(element("strong", "lineup-player", player.player_name || "名前不明"));
  addTeamContext(slot, player.team_name, player.league, player.matchup);
  slot.append(element("span", "lineup-eff", `${effLabel} ${formatMetric(player.efficiency)}`));
  return slot;
}

function addPredictionSlot(player) {
  const slot = element("div", "lineup-slot prediction-slot");
  slot.append(element("div", "lineup-role", `配置: ${player.role}`));
  slot.append(element("strong", "lineup-player", player.player_name || "名前不明"));
  addTeamContext(slot, player.team_name, player.league, player.matchup);
  slot.append(element("span", "lineup-eff", `試合前EFF ${formatMetric(player.efficiency)}`));
  slot.append(element("span", "prediction-actual", player.actual_efficiency === null
    ? "当日EFF —" : `当日EFF ${formatMetric(player.actual_efficiency)}`));
  return slot;
}

function addMatchupCard(matchup) {
  const card = element("article", "matchup-card");
  card.append(element("div", "matchup-league", matchup.league));
  const teams = element("div", "matchup-teams");
  const home = element("div", "matchup-team");
  home.append(element("span", "matchup-side", "HOME"));
  home.append(element("strong", "matchup-name", matchup.home_team_name));
  home.append(element("span", "matchup-record", `${matchup.home_wins}勝${matchup.home_losses}敗`));
  if (matchup.home_score !== null && matchup.home_score !== undefined) {
    home.append(element("span", "matchup-score", `${matchup.home_result} ${matchup.home_score}–${matchup.away_score}`));
  }
  const away = element("div", "matchup-team");
  away.append(element("span", "matchup-side", "AWAY"));
  away.append(element("strong", "matchup-name", matchup.away_team_name));
  away.append(element("span", "matchup-record", `${matchup.away_wins}勝${matchup.away_losses}敗`));
  if (matchup.away_score !== null && matchup.away_score !== undefined) {
    away.append(element("span", "matchup-score", `${matchup.away_result} ${matchup.away_score}–${matchup.home_score}`));
  }
  teams.append(home, element("span", "matchup-vs", "VS"), away);
  card.append(teams);
  return card;
}

function render(payload) {
  const periodLabel = payload.scope === "today"
    ? "本日対象 · 対戦チームの今季平均"
    : payload.scope === "season" ? "今シーズン平均" : formatDate(payload.date);
  selectedCaption.textContent = `${periodLabel} · ${payload.league === "all" ? "全リーグ" : payload.league}`;
  gameCount.textContent = payload.scope === "today"
    ? `本日 ${payload.game_count}試合`
    : payload.scope === "season" ? `対象 ${payload.game_count}試合` : `${payload.game_count}試合`;
  seasonAverageNote.hidden = !["season", "today"].includes(payload.scope);
  predictionDescription.textContent = "試合前までの各試合スタッツからEFFを算出して選び、当日の実績と比較しています。";
  lineupDescription.textContent = ["season", "today"].includes(payload.scope)
    ? "公式シーズン集計の平均EFFで選出。試合前予測とは集計元・算出基準が異なるため、同じ選手でも値が異なる場合があります。"
    : "ポジションを兼ねる選手も考慮し、同じ選手を重複させずに配置しています。";
  todayScopeNote.hidden = payload.scope !== "today";
  teamContextHeading.textContent = payload.scope === "season" ? "チーム" : "チーム・対戦";
  document.body.classList.toggle("daily-mode", payload.scope === "daily");
  gamesPlayedHeading.hidden = payload.scope === "daily";
  const statsUrl = new URL("https://www.bleague.jp/stats/");
  statsUrl.search = new URLSearchParams({ year: String(payload.season_year), tab: "1", target: "player-b1", value: "EFFPG", o: "desc", e: "2", dt: "avg" });
  officialSeasonLink.href = statsUrl.toString();
  matchupsGrid.replaceChildren(...(payload.matchups || []).map(addMatchupCard));
  matchupsBlock.hidden = payload.scope !== "today" || !(payload.matchups || []).length;
  const prediction = ["daily", "today"].includes(payload.scope) ? payload.prediction_lineup : null;
  predictionBlock.hidden = !prediction;
  if (prediction) {
    predictionGrid.replaceChildren(...prediction.players.map(addPredictionSlot));
    predictionGrid.hidden = false;
    predictionEmpty.hidden = true;
    const actual = prediction.actual_efficiency_total === null
      ? "未試合" : `${formatMetric(prediction.actual_efficiency_total)}（出場${prediction.actual_players_played}/5）`;
    const totalValue = element("div", "prediction-total-value");
    totalValue.append(element("strong", "", formatMetric(prediction.efficiency_total)));
    totalValue.append(element("span", "", `試合前／結果 ${actual}`));
    predictionTotal.replaceChildren(document.createTextNode("試合前EFF合計"), totalValue);
  } else {
    predictionGrid.replaceChildren();
    predictionGrid.hidden = true;
    predictionTotal.replaceChildren();
    predictionEmpty.hidden = true;
  }
  officialUpdatedAt.textContent = formatUpdatedAt(payload.season_updated_at);
  lineupHeading.textContent = ["season", "today"].includes(payload.scope)
    ? "平均EFF合計が最大になる5人"
    : "EFF合計が最大になる5人";
  overallBody.replaceChildren(...payload.overall.map(addOverallRow));
  overallEmpty.hidden = payload.overall.length !== 0;
  if (payload.best_lineup) {
    lineupTotal.replaceChildren(document.createTextNode(payload.scope === "season" ? "5人の平均EFF合計" : "5人のEFF合計"));
    lineupTotal.append(element("strong", "", formatMetric(payload.best_lineup.efficiency_total)));
    const lineupEffLabel = ["season", "today"].includes(payload.scope) ? "公式平均EFF" : "EFF";
    lineupGrid.replaceChildren(...payload.best_lineup.players.map(player => addLineupSlot(player, lineupEffLabel)));
    lineupGrid.hidden = false;
    lineupTotal.hidden = false;
    lineupEmpty.hidden = true;
  } else {
    lineupGrid.replaceChildren();
    lineupGrid.hidden = true;
    lineupTotal.hidden = true;
    lineupEmpty.hidden = false;
  }
  positionGrid.replaceChildren(...ROLE_ORDER.map(role => addPositionCard(role, payload.by_position[role] || [])));
  statusBox.hidden = true;
  results.hidden = false;
}

async function refreshRankings() {
  statusBox.className = "status";
  statusBox.textContent = "ランキングを読み込んでいます…";
  statusBox.hidden = false;
  try {
    const fileName = `${encodeURIComponent(dateSelect.value)}__${encodeURIComponent(leagueSelect.value)}.json`;
    render(await getJson(`./data/rankings/${fileName}`));
  } catch (error) {
    results.hidden = true;
    statusBox.className = "status error";
    statusBox.textContent = error.message;
  }
}

async function start() {
  try {
    const payload = await getJson("./data/dates.json");
    if (!payload.dates.length) throw new Error("試合日のデータがありません。");
    if (dataUpdatedAt) dataUpdatedAt.textContent = formatUpdatedAt(payload.generated_at);
    dateSelect.replaceChildren();
    addOption(dateSelect, "today", "本日対象（今季平均）");
    addOption(dateSelect, "season", "今季平均（全選手）");
    for (const date of payload.dates) addOption(dateSelect, date, formatDate(date));
    dateSelect.value = payload.dates[0];
    leagueSelect.replaceChildren();
    addOption(leagueSelect, "all", "全リーグ");
    for (const league of payload.leagues) addOption(leagueSelect, league, league);
    dateSelect.disabled = false;
    leagueSelect.disabled = false;
    await refreshRankings();
  } catch (error) {
    statusBox.className = "status error";
    statusBox.textContent = `${error.message} · 公開データの更新後に、もう一度お試しください。`;
  }
}

dateSelect.addEventListener("change", refreshRankings);
leagueSelect.addEventListener("change", refreshRankings);
start();
