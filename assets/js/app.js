(() => {
  "use strict";

  const cards = [...document.querySelectorAll(".competition-card")];
  const search = document.querySelector("#search");
  const teamFilter = document.querySelector("#team-filter");
  const categoryFilter = document.querySelector("#category-filter");
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

  if (!cards.length || !search || !teamFilter || !categoryFilter) return;

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
    let visibleCount = 0;

    cards.forEach((card) => {
      const matchesQuery = !query || `${card.dataset.title} ${card.dataset.teams}`.includes(query);
      const matchesTeam = !team || card.querySelector(`[data-team="${CSS.escape(team)}"]`);
      const matchesCategory = !category || card.dataset.category === category;
      const visible = Boolean(matchesQuery && matchesTeam && matchesCategory);
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
})();
