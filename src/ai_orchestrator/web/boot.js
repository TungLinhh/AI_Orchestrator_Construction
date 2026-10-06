// Skip to content preserves the selected route for keyboard users.
$("skipContent").onclick = (event) => {
  event.preventDefault();
  $("mainContent").focus?.();
};
/* ---------------- boot ---------------- */
(async function boot() {
  langSet(state.lang);
  try {
    await apiGet("/portfolio");
    setConn("ok", "conn.live", "live");
  } catch (err) {
    setConn("bad", "conn.none", "no connection");
    if (err && err.auth)
      toast(
        tr(
          "auth.need_token",
          "Not authorised. Click Token to enter the service token.",
        ),
        true,
      );
  }
  await loadScenarios();
  await loadOwners();
  // The bell restores its unread count from the last session, so a reload does not wipe
  // what has not been seen. Painted before the first render, not after the first event.
  paintBell();
  await refreshNotificationInbox();
  setInterval(refreshNotificationInbox, 5000);
  if (!window.location.hash && UIPreferences.get().home === "campaigns") window.location.hash = "#/processes/workflows";
  await route();
  if (state.es) state.es.abort();
  state.es = new AbortController();
  stream();
})();
