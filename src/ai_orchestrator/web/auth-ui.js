/* Authentication UI: human login, member display, signout. */
window.UIAuth = (() => {
  const L = UI.L;
  const $ = (id) => document.getElementById(id);

  let loginDialogEl = null;
  let loginFormBound = false;
  let loginPending = false;

  const ORG_ID_RE = /^org_[a-z0-9]{20,}$/;

  function openLoginDialog() {
    loginDialogEl = $("loginDialog");
    if (!loginDialogEl) return;

    // Prefill org ID from server-provided ORG constant
    const orgInput = $("loginTenant");
    if (orgInput && ORG && !orgInput.value) {
      orgInput.value = ORG;
    }

    bindLoginEventsOnce();
    loginDialogEl.showModal();
    $("loginEmail")?.focus();
  }

  function closeLoginDialog() {
    if (loginDialogEl) {
      loginDialogEl.close();
      const pw = loginDialogEl.querySelector("#loginPassword");
      if (pw) pw.value = "";
      const err = loginDialogEl.querySelector("#loginError");
      if (err) err.hidden = true;
    }
    loginPending = false;
  }

  function bindLoginEventsOnce() {
    if (loginFormBound) return;
    loginFormBound = true;

    const form = $("loginForm");
    const submitBtn = $("loginSubmit");
    const cancelBtn = $("loginCancel");
    const closeBtn = $("loginClose");

    if (cancelBtn) cancelBtn.addEventListener("click", closeLoginDialog);
    if (closeBtn) closeBtn.addEventListener("click", closeLoginDialog);

    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        if (!loginPending) handleLoginSubmit();
      });
    }

    // Native dialog handles Escape; ensure cleanup
    if (loginDialogEl) {
      loginDialogEl.addEventListener("close", () => {
        if (!loginPending) {
          const pw = loginDialogEl.querySelector("#loginPassword");
          if (pw) pw.value = "";
        }
      });
    }
  }

  async function handleLoginSubmit() {
    const form = $("loginForm");
    const submitBtn = $("loginSubmit");
    const errorEl = $("loginError");
    const tenantId = $("loginTenant")?.value?.trim();
    const email = $("loginEmail")?.value?.trim()?.toLowerCase();
    const password = $("loginPassword")?.value;

    if (!tenantId || !email || !password) {
      errorEl.textContent = L("Please fill in all fields.", "Vui lòng điền đầy đủ thông tin.");
      errorEl.hidden = false;
      return;
    }

    if (!ORG_ID_RE.test(tenantId)) {
      errorEl.textContent = L("Invalid organization ID format.", "Định dạng mã tổ chức không hợp lệ.");
      errorEl.hidden = false;
      return;
    }

    loginPending = true;
    submitBtn.disabled = true;
    submitBtn.setAttribute("aria-busy", "true");
    submitBtn.textContent = L("auth.logging_in", "Signing in…");
    errorEl.hidden = true;

    try {
      const data = await loginHuman(tenantId, email, password);
      const member = data.member;
      const accessToken = data.access_token;

      // Verify returned member tenant matches selection
      if (member.organization_id !== tenantId) {
        throw new Error(L("auth.tenant_mismatch", "Returned organization does not match selection."));
      }

      // Store token and member info
      setHumanToken(accessToken);
      saveMember(member);
      setOrgVerified(tenantId);

      // Update UI
      updateMemberDisplay(member);
      updateAuthActions(true);

      closeLoginDialog();

      // Reload current console with verified organization in URL
      if (ORG && ORG !== tenantId) {
        window.location.href = window.location.pathname + "?org=" + encodeURIComponent(tenantId) + window.location.hash;
      } else {
        await route();
      }

      toast(L("Signed in as {name}", "Đã đăng nhập với tư cách {name}", { name: member.display_name }), false);
    } catch (err) {
      errorEl.textContent = err.message || L("auth.login_failed", "Login failed. Check your credentials.");
      errorEl.hidden = false;
    } finally {
      loginPending = false;
      submitBtn.disabled = false;
      submitBtn.removeAttribute("aria-busy");
      submitBtn.textContent = L("auth.login_button", "Sign in");
    }
  }

  function updateMemberDisplay(member) {
    const nameEl = $("memberName");
    if (nameEl && member) {
      // Render as plain text, never interpolated innerHTML
      nameEl.textContent = member.display_name + " (" + member.email + ")";
    }
  }

  function updateAuthActions(signedIn) {
    const tokenBtn = $("tokenBtn");
    const loginBtn = $("loginBtn");
    const memberDisplay = $("memberDisplay");

    if (AUTH_OFF) {
      if (tokenBtn) tokenBtn.hidden = true;
      if (loginBtn) loginBtn.hidden = true;
      if (memberDisplay) memberDisplay.hidden = true;
      return;
    }

    if (signedIn) {
      if (tokenBtn) tokenBtn.hidden = true;
      if (loginBtn) loginBtn.hidden = true;
      if (memberDisplay) memberDisplay.hidden = false;
    } else {
      if (tokenBtn) tokenBtn.hidden = false;
      if (loginBtn) loginBtn.hidden = false;
      if (memberDisplay) memberDisplay.hidden = true;
    }
  }

  async function handleSignout() {
    const confirmed = confirm(L("auth.signout_confirm", "Signing out will revoke all current sessions. Continue?"));
    if (!confirmed) return;

    const btn = $("signoutBtn");
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.title = L("auth.signing_out", "Signing out…");

    try {
      await logoutHuman();
      toast(L("auth.signout_all", "Signed out of all sessions"), false);
    } catch (err) {
      // Honest error: do NOT claim server revocation on failure
      toast(L("Signout failed: {e}", "Đăng xuất thất bại: {e}", { e: err.message }), true);
      // Keep session for retry; do not clear
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.title = L("auth.signout", "Sign out");
      return;
    }
    // Clear local identity only after the server confirms revocation.
    clearHumanSession();
    updateAuthActions(false);
    updateMemberDisplay(null);
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    btn.title = L("auth.signout", "Sign out");
    window.location.reload();
  }

  async function verifySessionOnBoot() {
    // Called during boot to verify existing session
    if (AUTH_OFF) {
      updateAuthActions(false);
      return true; // development mode is ready without an identity session
    }

    const existingToken = token();
    const savedMember = getMember();
    const verifiedOrg = getOrgVerified();

    if (!existingToken) {
      updateAuthActions(false);
      return false;
    }

    // ALWAYS verify with /identity/me for human tokens - never trust cache
    try {
      const member = await fetchMe();
      // Verify tenant matches current ORG
      if (member.organization_id !== ORG) {
        // Token belongs to different tenant - clear and show login
        clearHumanSession();
        updateAuthActions(false);
        return false;
      }
      saveMember(member);
      setOrgVerified(member.organization_id);
      updateMemberDisplay(member);
      updateAuthActions(true);
      return true; // ready for protected loads
    } catch (err) {
      // Token invalid or expired
      clearHumanSession();
      updateAuthActions(false);
      return false;
    }
  }

  // Initialize event listeners
  function init() {
    $("loginBtn")?.addEventListener("click", openLoginDialog);
    $("signoutBtn")?.addEventListener("click", handleSignout);
    $("tokenBtn")?.addEventListener("click", () => {
      if (askToken(token()) !== null) {
        forgetToken();
        askToken("");
      }
    });
  }

  // Expose for boot integration
  return { init, verifySessionOnBoot, updateAuthActions, updateMemberDisplay, openLoginDialog };
})();