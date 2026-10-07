/* Only known local symbols are rendered; icons never carry accessible labels. */
const UIIcon = (name) => {
  const names = ["list-todo", "circle-alert", "circle-check", "network", "workflow", "library", "database", "activity", "settings", "bell", "panel-left-close", "panel-left-open", "arrow-left", "plus", "search", "copy", "chevron-down", "x", "clock", "pause", "play", "info", "circle-help", "check", "loader-circle", "sun", "moon", "monitor", "command", "sliders-horizontal"];
  const symbol = names.includes(name) ? name : "circle-help";
  return `<svg class="ui-icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><use href="#ui-icon-${symbol}"></use></svg>`;
};
