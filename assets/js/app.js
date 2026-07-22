(() => {
  "use strict";

  const cards = [...document.querySelectorAll(".competition-card")];
  const search = document.querySelector("#search");
  const teamFilter = document.querySelector("#team-filter");
  const categoryFilter = document.querySelector("#category-filter");
  const stateFilter = document.querySelector("#state-filter");
  const resultCount = document.querySelector("#result-count");
  const emptyState = document.querySelector("#filter-empty");

  const formatDate = (value, withTime = false) => {
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return value;
    const options = withTime
      ? {
          year: "numeric",
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          timeZoneName: "short",
        }
      : { year: "numeric", month: "short", day: "numeric" };
    return new Intl.DateTimeFormat("zh-CN", options).format(date);
  };

  document.querySelectorAll(".local-date").forEach((element) => {
    element.textContent = formatDate(element.dateTime);
  });
  document.querySelectorAll(".local-time").forEach((element) => {
    element.textContent = formatDate(element.dateTime, true);
  });

  const teamBoardTabs = [...document.querySelectorAll("[data-team-board-target]")];
  const teamBoards = [...document.querySelectorAll("[data-team-board]")];
  const selectTeamBoard = (name, focus = false) => {
    teamBoardTabs.forEach((tab) => {
      const selected = tab.dataset.teamBoardTarget === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    });
    teamBoards.forEach((board) => {
      board.hidden = board.dataset.teamBoard !== name;
    });
  };
  teamBoardTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTeamBoard(tab.dataset.teamBoardTarget));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const nextIndex = (index + offset + teamBoardTabs.length) % teamBoardTabs.length;
      selectTeamBoard(teamBoardTabs[nextIndex].dataset.teamBoardTarget, true);
    });
  });
  if (teamBoardTabs.length) selectTeamBoard("overall");

  if (!cards.length || !search || !teamFilter || !categoryFilter || !stateFilter) return;

  [...new Set(cards.map((card) => card.dataset.category).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"))
    .forEach((category) => {
      const option = document.createElement("option");
      option.value = category;
      option.textContent = category;
      categoryFilter.append(option);
    });

  const applyFilters = () => {
    const query = search.value.trim().toLocaleLowerCase();
    const team = teamFilter.value;
    const category = categoryFilter.value;
    const state = stateFilter.value;
    let visibleCount = 0;

    cards.forEach((card) => {
      const matchesQuery = !query || `${card.dataset.title} ${card.dataset.teams}`.includes(query);
      const matchesTeam = !team || card.querySelector(`[data-team="${CSS.escape(team)}"]`);
      const matchesCategory = !category || card.dataset.category === category;
      const matchesState = !state || card.dataset.state === state;
      const visible = Boolean(matchesQuery && matchesTeam && matchesCategory && matchesState);
      card.hidden = !visible;
      if (visible) visibleCount += 1;

      card.querySelectorAll("tbody tr[data-team]").forEach((row) => {
        row.hidden = Boolean(team && row.dataset.team !== team);
      });
    });

    if (resultCount) resultCount.textContent = `显示 ${visibleCount} / ${cards.length} 场`;
    if (emptyState) emptyState.hidden = visibleCount !== 0;
  };

  search.addEventListener("input", applyFilters);
  teamFilter.addEventListener("change", applyFilters);
  categoryFilter.addEventListener("change", applyFilters);
  stateFilter.addEventListener("change", applyFilters);
})();
