// Live scores: elements with data-live-game (slate rows, home cards, the game scoreboard) are
// refreshed every 30 s from /api/live/<league> while any of them is live or about to start.
// The game page's #live-panel (situation, win-probability chart, play-by-play) is re-fetched too.
(function () {
  "use strict";
  var INTERVAL = 30000;
  var SOON = 30 * 60; // seconds before kickoff to start polling

  function games() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-live-game]"));
  }

  function wanted(el) {
    var state = el.dataset.state;
    if (state === "in") return true;
    if (state !== "pre") return false;
    var kickoff = parseInt(el.dataset.kickoff || "0", 10);
    return kickoff && kickoff - Date.now() / 1000 < SOON;
  }

  function badge(detail) {
    return '<span class="live-dot">LIVE</span> ' + escapeHtml(detail || "");
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function apply(el, g) {
    el.dataset.state = g.state;
    var status = el.querySelector(".js-status");
    if (status) {
      status.innerHTML = g.state === "in" ? badge(g.detail) : escapeHtml(g.state === "final" ? (g.detail || "Final") : "");
      status.className = status.className.replace(/\bmuted\b/, "") + (g.state === "in" ? " live-status" : "");
    }
    var score = el.querySelector(".js-score");
    if (score && g.home !== null) score.textContent = g.away + "–" + g.home;
    // Scoreboard / home card: separate away and home cells. Pre-game cards show projections
    // in .gc-proj; switch those to live scores.
    var away = el.querySelector(".js-away"), home = el.querySelector(".js-home");
    if (!away || !home) {
      var cells = el.querySelectorAll(".gc-proj");
      if (cells.length === 2) {
        away = cells[0]; home = cells[1];
        [away, home].forEach(function (c) { c.className = "gc-live"; });
      }
    }
    if (away && home && g.home !== null) {
      away.textContent = g.away;
      home.textContent = g.home;
    }
  }

  function refreshPanel(done) {
    var panel = document.querySelector("[data-live-panel]");
    if (!panel) return;
    fetch(panel.dataset.livePanel, {cache: "no-store"})
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) panel.innerHTML = html; })
      .catch(function () {})
      .then(done || function () {});
  }

  function tick() {
    if (document.hidden) return;
    var active = games().filter(wanted);
    if (!active.length) return;
    var byLeague = {};
    active.forEach(function (el) {
      (byLeague[el.dataset.league] = byLeague[el.dataset.league] || []).push(el);
    });
    Object.keys(byLeague).forEach(function (league) {
      var els = byLeague[league];
      var ids = els.map(function (el) { return el.dataset.liveGame; }).join(",");
      fetch("/api/live/" + league + "?ids=" + ids, {cache: "no-store"})
        .then(function (r) { return r.ok ? r.json() : {}; })
        .then(function (data) {
          els.forEach(function (el) { if (data[el.dataset.liveGame]) apply(el, data[el.dataset.liveGame]); });
        })
        .catch(function () {});
    });
    refreshPanel();
  }

  if (games().length) {
    setInterval(tick, INTERVAL);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
  }
})();
