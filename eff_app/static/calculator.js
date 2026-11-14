"use strict";
const RARITY_BONUS = [0, 100, 100, 130, 150, 200];
const TEAM_BONUS = [0, 0, 1, 3, 5, 10];
const form = document.querySelector("#calculator-form");
const slots = document.querySelector("#calculator-slots");
const format = value => value.toLocaleString("ja-JP", { maximumFractionDigits: 2 });

function calculateScore(players, enabled) {
  const counts = {};
  let raw = 0, adjusted = 0, winners = 0;
  const values = players.map(player => {
    raw += player.eff;
    const value = player.eff * (1 + (enabled.card ? RARITY_BONUS[player.rarity] : 0) / 100);
    adjusted += value;
    winners += Number(player.won);
    counts[player.team] = (counts[player.team] || 0) + 1;
    return value;
  });
  const largest = Math.max(0, ...Object.values(counts));
  const winPercent = enabled.win ? winners * 10 : 0;
  const teamPercent = enabled.team ? TEAM_BONUS[largest] : 0;
  return { raw, adjusted, winners, largest, winPercent, teamPercent, values,
    total: adjusted * (1 + (winPercent + teamPercent) / 100) };
}

function renderCalculator() {
  const rows = [...slots.querySelectorAll(".calculator-player")];
  const valid = rows.every(row => row.querySelector("input[type=number]").value.trim() !== ""
    && row.querySelector("input[type=number]").validity.valid);
  document.querySelector("#calculator-error").hidden = valid;
  document.querySelector("#calculator-result").hidden = !valid;
  if (!valid) return;
  const players = rows.map(row => ({
    eff: Number(row.querySelector(".calc-eff").value),
    rarity: Number(row.querySelector(".calc-rarity").value),
    team: row.querySelector(".calc-team").value,
    won: row.querySelector(".calc-won").checked,
  }));
  const result = calculateScore(players, {
    card: document.querySelector("#enable-card").checked,
    win: document.querySelector("#enable-win").checked,
    team: document.querySelector("#enable-team").checked,
  });
  rows.forEach((row, i) => { row.querySelector("output").textContent = format(result.values[i]); });
  document.querySelector("#calc-total").textContent = format(result.total);
  document.querySelector("#calc-raw").textContent = format(result.raw);
  document.querySelector("#calc-card-total").textContent = format(result.adjusted);
  document.querySelector("#calc-win").textContent = `${result.winPercent}%（勝利チーム所属 ${result.winners}人）`;
  document.querySelector("#calc-team").textContent = `${result.teamPercent}%（同チーム最大 ${result.largest}人）`;
  document.querySelector("#calc-formula").textContent = `${format(result.adjusted)} ×（1 ＋ ${result.winPercent}% ＋ ${result.teamPercent}%）＝ ${format(result.total)}`;
}

function synchronizeTeam(target) {
  const row = target.closest(".calculator-player");
  if (!row) return;
  const team = row.querySelector(".calc-team").value;
  const peers = [...slots.querySelectorAll(".calculator-player")].filter(other => other !== row
    && other.querySelector(".calc-team").value === team);
  if (target.classList.contains("calc-team") && peers.length) {
    row.querySelector(".calc-won").checked = peers[0].querySelector(".calc-won").checked;
  } else if (target.classList.contains("calc-won")) {
    peers.forEach(other => { other.querySelector(".calc-won").checked = target.checked; });
  }
}

for (let i = 0; i < 5; i += 1) {
  const row = document.createElement("fieldset");
  row.className = "calculator-player";
  row.innerHTML = `<legend>枠 ${i + 1}</legend>
    <label>仮定するEFF<input class="calc-eff" type="number" step="any" value="30" required aria-label="枠${i + 1}のEFF"></label>
    <label>レアリティ<select class="calc-rarity" aria-label="枠${i + 1}のレアリティ">
      <option value="0">なし（+0%）</option><option value="1">★（+100%）</option>
      <option value="2">★★（+100%）</option><option value="3">★★★（+130%）</option>
      <option value="4">★★★★（+150%）</option><option value="5">★★★★★（+200%）</option></select></label>
    <label>チーム<select class="calc-team" aria-label="枠${i + 1}のチーム">${["A", "B", "C", "D", "E"].map((team, index) => `<option${index === i ? " selected" : ""}>${team}</option>`).join("")}</select></label>
    <label class="calc-win-label"><input type="checkbox" class="calc-won" aria-label="枠${i + 1}のチームが勝利">勝利</label>
    <div class="calc-player-result">カード反映後<output aria-label="枠${i + 1}のカード反映後スコア">30</output></div>`;
  slots.append(row);
}
form.addEventListener("input", event => { synchronizeTeam(event.target); renderCalculator(); });
form.addEventListener("submit", event => event.preventDefault());
form.addEventListener("reset", () => { setTimeout(renderCalculator, 0); });
renderCalculator();
