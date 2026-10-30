/* VyapAI frontend — onboarding → real login (PIN + Telegram-OTP) → dashboard.
   Vanilla JS, no build. Server-side sessions: opaque bearer token kept in
   localStorage; every API call carries it; a 401 drops you back to login. */
"use strict";

const API = ""; // same origin
const KEY_TOKEN = "vyapai_token";

const $ = (id) => document.getElementById(id);
const SCREEN_ONBOARD = $("onboarding");
const SCREEN_LOGIN = $("login");
const SCREEN_DASH = $("dashboard");

// ---------- helpers ----------
function fmtInr(x) {
  const n = Number(x) || 0;
  return "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function authHeader() {
  const t = localStorage.getItem(KEY_TOKEN);
  return t ? { Authorization: "Bearer " + t } : {};
}

function signOutLocal() {
  localStorage.removeItem(KEY_TOKEN);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json", ...authHeader() },
    ...opts,
  });
  if (res.status === 401) {
    // Session expired or never valid → back to login, everywhere.
    signOutLocal();
    showLogin();
    throw new Error("Please sign in again");
  }
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail ?? ""; } catch { /* non-json */ }
    throw new Error(
      typeof detail === "string" ? (detail || `HTTP ${res.status}`) : JSON.stringify(detail)
    );
  }
  return res.json();
}

function setFeedback(el, text, ok) {
  el.textContent = text;
  el.className = "feedback " + (ok ? "ok" : "bad");
}

// ---------- toasts (bottom-right, auto-dismiss) ----------
function toast(msg, kind = "ok") {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  host.appendChild(t);
  requestAnimationFrame(() => t.classList.add("in"));
  setTimeout(() => {
    t.classList.remove("in");
    setTimeout(() => t.remove(), 300);
  }, 3500);
}

function showScreen(el) {
  [SCREEN_ONBOARD, SCREEN_LOGIN, SCREEN_DASH].forEach((s) => s.classList.add("hidden"));
  el.classList.remove("hidden");
}

function showFormError(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
}

function chip(status) {
  const s = (status || "—").replace(/_/g, " ");
  const cls = ["paid", "confirmed", "pending", "failed", "out_of_stock", "rejected"].includes(status)
    ? status : "";
  return `<span class="chip ${cls}">${s}</span>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- onboarding (new shops) ----------
// Shared tab logic for BOTH screens: each tab set exposes data-channel
// phone|email and toggles the matching identity input.
function wireIdentityTabs(tabsEl, phoneLabelId, emailLabelId) {
  tabsEl.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      tabsEl.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const email = tab.dataset.channel === "email";
      $(email ? emailLabelId : phoneLabelId).classList.remove("hidden");
      $(email ? phoneLabelId : emailLabelId).classList.add("hidden");
    });
  });
}

function activeChannel(tabsEl) {
  const at = tabsEl.querySelector(".tab.active");
  return at ? at.dataset.channel : "phone";
}

wireIdentityTabs($("ob-tabs"), "ob-phone-label", "ob-email-label");
wireIdentityTabs($("lg-tabs"), "lg-phone-label", "lg-email-label");

$("onboarding-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("ob-error");
  err.classList.add("hidden");
  const channel = activeChannel($("ob-tabs"));
  const pin = $("ob-pin").value.trim();
  const identityInput = channel === "email" ? $("ob-email") : $("ob-wa");
  const identity = identityInput.value.trim();
  if (!/^\d{4,8}$/.test(pin)) {
    showFormError(err, "PIN must be 4–8 digits (numbers only).");
    return;
  }
  if (!identity) {
    showFormError(err, (channel === "email" ? "Email" : "Phone number") + " is required — it's how your login code is delivered.");
    return;
  }
  const payload = {
    business_name: $("ob-name").value.trim(),
    gstin: $("ob-gstin").value.trim() || null,
    upi_vpa: $("ob-vpa").value.trim() || null,
    preferred_language: $("ob-lang").value,
    pin,
  };
  if (channel === "email") payload.email = identity;
  else payload.whatsapp_number = identity;
  $("ob-submit").disabled = true;
  try {
    // 1) create the shop; 2) immediately fire the OTP to the identity the
    // shopkeeper signed up with; 3) verify it on the login screen's verify
    // step (code only) — same flow, whether phone or email.
    const merchant = await api("/b2b/merchants", { method: "POST", body: JSON.stringify(payload) });
    const otpReq = channel === "email"
      ? await requestOtp("email", merchant.email)
      : await requestOtp("phone", merchant.whatsapp_number);
    if (!otpReq) return;
    await showVerifyStep(otpReq, `Shop created! We sent a 6-digit code to ${otpReq.sent_to || identity}.`);
  } catch (ex) {
    showFormError(err, "Could not create the shop: " + ex.message);
  } finally {
    $("ob-submit").disabled = false;
  }
});

// ---------- login (returning merchants) ----------
let loginChallenge = null;

async function requestOtp(channel, value, errEl) {
  const err = errEl || $("lg-error");
  err.classList.add("hidden");
  try {
    return await api("/b2b/auth/otp/request", {
      method: "POST",
      body: JSON.stringify({ channel, value }),
    });
  } catch (ex) {
    showFormError(err, ex.message);
    return null;
  }
}

// Flip the login screen to the code-verify step for the returned challenge.
function showVerifyStep(r, headline) {
  loginChallenge = r;
  showScreen(SCREEN_LOGIN);
  $("login-step-number").classList.add("hidden");
  $("login-step-verify").classList.remove("hidden");
  if (r.delivery === "dev_echo") {
    $("lg-code-sent").textContent = "Your login code (send channel not configured yet — dev mode delivers it here):";
    $("lg-otp").value = r.otp; // dev-only convenience
    $("lg-hint-dev").textContent = "dev_echo: set TELEGRAM_BOT_TOKEN / SMTP_* on the server and the code arrives in the real channel instead.";
  } else {
    $("lg-code-sent").textContent = (headline || `Code sent to ${r.sent_to}`) +
      ` (${Math.round(r.expires_in_seconds / 60)} min validity).`;
    $("lg-hint-dev").textContent = "";
  }
}

$("login-form-number").addEventListener("submit", async (e) => {
  e.preventDefault();
  const channel = activeChannel($("lg-tabs"));
  const identity = (channel === "email" ? $("lg-email") : $("lg-wa")).value.trim();
  if (!identity) {
    showFormError($("lg-error"), (channel === "email" ? "Email" : "Phone number") + " is required.");
    return;
  }
  $("lg-send").disabled = true;
  const r = await requestOtp(channel, identity);
  $("lg-send").disabled = false;
  if (!r) return;
  showVerifyStep(r);
});

$("login-form-verify").addEventListener("submit", async (e) => {
  e.preventDefault();
  await verifyLogin(
    loginChallenge && loginChallenge.challenge_id,
    $("lg-otp").value.trim(),
    $("lg-error-verify"),
  );
});

$("lg-resend").addEventListener("click", () => {
  $("login-step-verify").classList.add("hidden");
  $("login-step-number").classList.remove("hidden");
  $("lg-otp").value = "";
});

async function verifyLogin(challengeId, code, errEl) {
  errEl = errEl || $("lg-error-verify");
  errEl.classList.add("hidden");
  if (!challengeId || !code) {
    showFormError(errEl, "The 6-digit code is required.");
    return;
  }
  $("lg-verify") && ($("lg-verify").disabled = true);
  try {
    const r = await api("/b2b/auth/otp/verify", {
      method: "POST",
      body: JSON.stringify({ challenge_id: challengeId, code }),
    });
    localStorage.setItem(KEY_TOKEN, r.token);
    await enterDashboard(r.merchant);
  } catch (ex) {
    showFormError(errEl, ex.message);
  } finally {
    $("lg-verify") && ($("lg-verify").disabled = false);
  }
}

function showLogin() {
  showScreen(SCREEN_LOGIN);
  $("login-step-number").classList.remove("hidden");
  $("login-step-verify").classList.add("hidden");
  $("lg-otp").value = "";
  $("lg-wa").value = "";
  $("lg-email").value = "";
}

// Screen switchers so BOTH entry points stay reachable:
// login → register and register → login.
$("go-register").addEventListener("click", () => showScreen(SCREEN_ONBOARD));
$("go-login").addEventListener("click", () => showLogin());

// ---------- channels (Telegram + WhatsApp) ----------
async function renderChannels() {
  try {
    const st = await api(`/b2b/channels?merchant_id=${MERCHANT_ID}`);
    const paint = (ch, chipEl, detailEl) => {
      const badge = chipEl;
      badge.className = "chip " + (ch.ready ? "paid" : ch.configured || ch.linked ? "pending" : "failed");
      badge.textContent = ch.ready ? "Ready" : ch.configured ? "not linked" : ch.linked ? "no bot token" : "not set up";
      detailEl.textContent = ch.ready
        ? "Linked and configured — OTP codes / order replies arrive here."
        : ch.setup_hint ? "Add " + ch.setup_hint + " to the server env to enable sending."
        : "Configured — link your chat to activate it for this shop.";
    };
    paint(st.telegram, $("tg-status"), $("tg-detail"));
    paint(st.whatsapp, $("wa-status"), $("wa-detail"));
    return st;
  } catch { /* card stays 'checking…' on failure — non-critical surface */ }
}$( "tg-link-btn" ).addEventListener( "click", async () => {
  const btn = $( "tg-link-btn" );
  const stepper = $("tg-wizard");
  btn.disabled = true;
  try {
    const ch = await api( `/b2b/channels?merchant_id=${MERCHANT_ID}` );
    // Mint a pairing code for this shop.
    const r = await api( "/b2b/auth/telegram/link", { method: "POST", body: "{}" } );
    const botUser = ch.telegram.bot_username;
    const target = botUser ? `@${botUser}` : "the VyapAI bot";
    const link = botUser
      ? `https://t.me/${botUser}?start=${r.code}`
      : null;
    stepper.classList.remove( "hidden" );
    stepper.innerHTML = `
      <div class="wiz-step">
        <div class="wiz-num">1</div>
        <div class="wiz-body">
          <div><strong>Open Telegram</strong> and send this to ${esc(target)}:</div>
          <div class="wiz-code">/start ${r.code} <button class="btn tiny ghost" data-copy="/start ${r.code}">📋 Copy</button></div>
          ${link ? `<a class="btn tiny primary" href="${esc(link)}" target="_blank" rel="noopener">⚡ One-tap open in Telegram</a>` : ""}
        </div>
      </div>
      <div class="wiz-step">
        <div class="wiz-num" id="wiz-num-2">2</div>
        <div class="wiz-body">
          <div id="wiz-waiting"><span class="spinner"></span> Waiting for your message… (polls every 3s, up to 2 min)</div>
        </div>
      </div>`;
    stepper.querySelectorAll( "[data-copy]" ).forEach( b =>
      b.addEventListener( "click", () => {
        navigator.clipboard.writeText( b.dataset.copy ).then( () => {
          b.textContent = "✓ Copied";
          setTimeout( () => ( b.textContent = "📋 Copy" ), 1500 );
        } );
      } )
    );
    // If no real bot exists yet, the /start cannot arrive — offer the
    // server-side demo-link (exact webhook code path) as an escape hatch.
    if ( !ch.telegram.configured ) {
      stepper.querySelector("#wiz-waiting").insertAdjacentHTML( "beforeend",
        `<div class="smallhint">No bot token on this server yet — <button class="btn tiny approve" id="wiz-demo">simulate /start (demo link)</button></div>` );
      stepper.querySelector("#wiz-demo").addEventListener( "click", async () => {
        const demo = await api( "/webhooks/telegram/demo-link", { method: "POST", body: "{}" } );
        if ( demo.linked ) {
          toast( "Telegram chat linked (demo mode)", "ok" );
          stepper.classList.add( "hidden" );
          await renderChannels();
        }
      } );
    }
    // Live poll — both the pairing status AND the server's getUpdates pull,
    // so a real /start lands even though localhost can't receive webhooks.
    const deadline = Date.now() + 120000;
    const poll = setInterval( async () => {
      try {
        await api( "/webhooks/telegram/poll", { method: "POST", body: "{}" } );
      } catch { /* no bot token yet — keep polling */ }
      try {
        const st = await api( `/b2b/auth/telegram/link/${r.challenge_id}` );
        if ( st.linked ) {
          clearInterval( poll );
          stepper.querySelector("#wiz-num-2").innerHTML = "✓";
          stepper.querySelector("#wiz-waiting").innerHTML =
            `<strong style="color:var(--green-dark)">Linked! Login codes will arrive in your Telegram.</strong>`;
          toast( "Telegram linked — OTP codes now come to your chat", "ok" );
          btn.textContent = "✓ Linked";
          await renderChannels();
          setTimeout( () => stepper.classList.add( "hidden" ), 2500 );
          return;
        }
      } catch { /* keep polling until deadline */ }
      if ( Date.now() > deadline ) {
        clearInterval( poll );
        stepper.querySelector("#wiz-waiting").innerHTML =
          `<span style="color:var(--amber)">Timed out — code expired or /start never arrived.</span> <button class="btn tiny ghost" id="wiz-retry">↻ Try again</button>`;
        stepper.querySelector("#wiz-retry").addEventListener( "click", () => {
          stepper.classList.add( "hidden" );
          btn.click();
        } );
        btn.textContent = "Link via /start code";
      }
    }, 3000 );
  } catch (ex) {
    btn.textContent = "Link via /start code";
    toast( "Telegram link failed: " + ex.message, "bad" );
  } finally {
    btn.disabled = false;
  }
} );

// Demo helper: no Meta number exists here yet, so the dashboard offers a
// server-side simulation of the shop's own first WhatsApp message — the
// exact code path the real webhook runs.
$("wa-link-btn").addEventListener("click", async () => {
  const btn = $("wa-link-btn");
  btn.disabled = true;
  try {
    const r = await api("/webhooks/whatsapp/demo-link", {
      method: "POST",
      body: JSON.stringify({ merchant_id: MERCHANT_ID }),
    });
    btn.textContent = r.linked ? "✓ Linked via first contact" : "Couldn't link";
    await renderChannels();
  } catch (ex) {
    btn.textContent = "Simulate first-contact link";
    alert("WhatsApp link failed: " + ex.message);
  } finally {
    btn.disabled = false;
  }
});

let dashLangValue = "hi"; // last known-good selection, restored if a save fails

$("dash-lang").addEventListener("change", async () => {
  const sel = $("dash-lang");
  const value = sel.value;
  const restore = dashLangValue; // drop back here when the save fails
  sel.disabled = true;
  try {
    const m = await api("/b2b/auth/me/language", {
      method: "PATCH",
      body: JSON.stringify({ preferred_language: value }),
    });
    dashLangValue = m.preferred_language;
    applyI18n(dashLangValue);              // the whole interface follows the choice
    await refreshAll();                    // dynamic strings (tables, buttons) re-render too
    await renderChannels();
    toast(t("toast_lang").replace("{name}", sel.options[sel.selectedIndex].text), "ok");
  } catch (ex) {
    sel.value = restore;
    toast("Couldn't change language: " + ex.message, "bad");
  } finally {
    sel.disabled = false;
  }
});

// ---------- help modal ----------
const helpModal = $("help-modal");
function openHelp()  { helpModal.classList.remove("hidden"); }
function closeHelp() { helpModal.classList.add("hidden"); }
$("help-btn").addEventListener("click", openHelp);
$("help-close-x").addEventListener("click", closeHelp);
$("help-backdrop").addEventListener("click", closeHelp);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHelp(); });

// ---------- sign out ----------
$("switch-shop").addEventListener("click", async () => {
  try { await api("/b2b/auth/logout", { method: "POST", body: "{}" }); } catch { /* already gone */ }
  signOutLocal();
  showLogin();
});


// ---------- dashboard ----------
let MERCHANT_ID = null;

async function enterDashboard(merchant) {
  MERCHANT_ID = merchant.id;
  showScreen(SCREEN_DASH);
  $("shop-name").textContent = merchant.business_name || merchant.id.slice(0, 8);
  if (merchant.preferred_language) {
    dashLangValue = merchant.preferred_language;
    $("dash-lang").value = merchant.preferred_language;
  }
  applyI18n(dashLangValue);                // render the dashboard in the shop's language
  closeHelp();
  $("tg-link-btn").textContent = "Link via /start code";
  $("wa-link-btn").textContent = "Simulate first-contact link";
  await refreshAll();
  await renderChannels();
}

async function refreshAll() {
  const [ledger, orders, approvals] = await Promise.all([
    api(`/b2b/merchants/${MERCHANT_ID}/ledger`),
    api(`/b2b/merchants/${MERCHANT_ID}/orders`),
    api(`/b2b/merchants/${MERCHANT_ID}/approvals`),
  ]);
  renderStats(ledger, orders);
  renderLedger(ledger);
  renderOrders(orders);
  renderApprovals(approvals);
}

function renderStats(ledger, orders) {
  let sales = 0, purch = 0;
  for (const e of ledger) {
    if (e.account === "Sales") sales += e.amount;      // one row per sale (money in)
    else if (e.account === "Purchase") purch += e.amount;
  }
  $("stat-sales").textContent = fmtInr(sales);
  $("stat-purch").textContent = fmtInr(purch);
  $("stat-orders").textContent = orders.length;
  // Money the shop is waiting on: confirmed orders not yet settled — UPI
  // payments confirm themselves, so what lingers here is unconfirmed cash.
  $("stat-pending").textContent = fmtInr(
    orders.filter((o) => o.status === "confirmed" && o.payment_status !== "success")
          .reduce((s, o) => s + o.total_amount, 0)
  );
}

function renderLedger(ledger) {
  const tbody = $("ledger-table").querySelector("tbody");
  tbody.innerHTML = ledger
    .map((e) => `<tr>
      <td class="muted">${e.created_at ? e.created_at.slice(0, 16).replace("T", " ") : ""}</td>
      <td>${esc(e.account)}</td>
      <td class="muted">${esc(e.memo || "")}</td>
      <td class="${e.account === "Sales" ? "credit" : "debit"}"><strong>${fmtInr(e.amount)}</strong></td>
      <td>${e.gst_rate ? e.gst_rate + "%" : "—"}</td>
      <td class="muted">${esc(e.source)}</td>
    </tr>`)
    .join("") || `<tr><td colspan="6" class="muted">${t("empty_ledger")}</td></tr>`;
}

function offerCell(o) {
  if (!o.offer_type || !o.offer_value) return '<span class="muted">—</span>';
  const shown = o.offer_type === "percent" ? `${fmtQty(o.offer_value)}% off` : `₹${fmtQty(o.offer_value)} off`;
  return `<span class="chip">${esc(shown)}</span><div class="muted" style="font-size:11.5px">− ₹${fmtQty(o.discount_amount)}</div>`;
}

const fmtQty = (n) => (Number(n) % 1 === 0 ? String(Math.trunc(Number(n))) : String(Number(n)));

function renderOrders(orders) {
  const tbody = $("orders-table").querySelector("tbody");
  tbody.innerHTML = orders
    .map((o) => `<tr>
      <td class="muted">${o.created_at ? o.created_at.slice(0, 16).replace("T", " ") : ""}</td>
      <td>${esc((o.line_items || []).map((li) => `${li.quantity} ${li.unit} ${li.item_name}`).join(", ")) || esc(o.raw_message).slice(0, 40)}</td>
      <td class="muted">${esc(o.buyer)}</td>
      <td>${offerCell(o)}</td>
      <td><strong>${fmtInr(o.total_amount)}</strong></td>
      <td>${chip(o.status)}</td>
      <td>${o.payment_status ? chip(o.payment_status) + (o.payment_method ? ` <span class="muted" style="font-size:11px">${o.payment_method}</span>` : "") : '<span class="muted">—</span>'}</td>
      <td>${o.status === "confirmed" && o.payment_status !== "success"
        ? `<button class="btn tiny cash" data-cash="${o.id}">${t("btn_cash")}</button>`
        : ""}${o.upi_deep_link && o.payment_status !== "success"
        ? `<a class="upi-link" href="${esc(o.upi_deep_link)}">${t("upi_link")}</a>`
        : ""}</td>
    </tr>`)
    .join("") || `<tr><td colspan="8" class="muted">${t("empty_orders")}</td></tr>`;

  tbody.querySelectorAll("button[data-cash]").forEach((b) =>
    b.addEventListener("click", () => confirmCash(b.dataset.cash))
  );
}

async function confirmCash(orderId) {
  // Money in hand: the merchant confirms the cash on the spot (UPI payments
  // confirm themselves the moment their link is generated).
  await api("/webhooks/payments/cash", {
    method: "POST",
    body: JSON.stringify({ order_id: orderId }),
  });
  toast(t("toast_cash"), "ok");
  await refreshAll();
  // Flash the updated stat cards.
  document.querySelectorAll(".stat").forEach((el) => {
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 700);
  });
}

function renderApprovals(approvals) {
  const list = $("approvals-list");
  list.innerHTML = approvals
    .map((a) => {
      const isPending = a.status === "pending";
      return `<div class="approval">
        <div>
          <div>${esc(a.action_type)} · <strong>${chip(a.status)}</strong></div>
          ${a.reason ? `<div class="why">Why held: ${esc(a.reason)}</div>` : ""}
          <div class="muted" style="font-size:11.5px;margin-top:2px">${a.created_at ? a.created_at.slice(0, 16).replace("T", " ") : ""}</div>
        </div>
        ${isPending
          ? `<div class="acts">               <button class="btn tiny approve" data-id="${a.id}" data-approve="1">${t("approve")}</button>
               <button class="btn tiny reject" data-id="${a.id}" data-approve="0">${t("reject")}</button>
              </div>`
          : `<div class="done">${t("decided")}</div>`}
      </div>`;
    })
    .join("") || `<p class="muted">${t("approv_none")}</p>`;

  list.querySelectorAll("button[data-id]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/b2b/approvals/${b.dataset.id}/decide?approve=${b.dataset.approve}`, { method: "POST" });
      toast(b.dataset.approve === "1" ? t("toast_approved") : t("toast_rejected"), "ok");
      await refreshAll();
    })
  );
}

// ---------- forms ----------
$("entry-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fb = $("entry-feedback");
  try {
    const out = await api("/b2b/entries/text", {
      method: "POST",
      body: JSON.stringify({
        merchant_id: MERCHANT_ID,
        party_name: $("entry-party").value.trim(),
        transaction_kind: $("entry-kind").value,
        taxable_value: parseFloat($("entry-amount").value),
        gst_rate: parseFloat($("entry-gst").value),
      }),
    });
    if (out.needs_human_approval) {
      setFeedback(fb, t("toast_held"), false);
      toast(t("toast_held"), "warn");
    } else {
      const posted = t("toast_posted").replace("{g}", out.transaction_group.slice(0, 8));
      setFeedback(fb, posted, true);
      toast(posted, "ok");
    }
    $("entry-party").value = ""; $("entry-amount").value = "";
    await refreshAll();
  } catch (ex) {
    setFeedback(fb, "Couldn't post: " + ex.message, false);
  }
});

$("order-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fb = $("order-feedback");
  const offerType = $("offer-type").value;
  const offerRaw = parseFloat($("offer-value").value);
  const offer = ($.trim($("offer-value").value) && offerRaw > 0)
    ? { offer_type: offerType, offer_value: offerRaw } : {};
  try {
    const out = await api(`/b2c/merchants/${MERCHANT_ID}/orders`, {
      method: "POST",
      body: JSON.stringify({
        from_number: $("order-buyer").value.trim(),
        message_text: $("order-text").value.trim(),
        ...offer,
      }),
    });
    const lines = [
      out.status === "confirmed" ? t("toast_order_confirmed") : t("toast_flagged") + out.status,
      out.total_amount ? t("toast_total").replace("{x}", fmtInr(out.total_amount)) : "",
      out.discount_amount ? t("toast_offer").replace("{x}", fmtInr(out.discount_amount)).replace("{t}", offer.offer_type) : "",
      out.upi_link ? t("toast_upisent") : "",
    ].filter(Boolean);
    setFeedback(fb, lines.join(" · "), out.status === "confirmed");
    toast(out.status === "confirmed" ? t("toast_order_confirmed") : t("toast_flagged") + out.status,
          out.status === "confirmed" ? "ok" : "warn");
    await refreshAll();
  } catch (ex) {
    setFeedback(fb, "Order failed: " + ex.message, false);
  }
});

$("loan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fb = $("loan-feedback");
  try {
    const out = await api(
      `/b2b/merchants/${MERCHANT_ID}/loan-readiness?requested_amount=${encodeURIComponent($("loan-amount").value)}&purpose=${encodeURIComponent($("loan-purpose").value.trim())}`
    );
    fb.textContent = JSON.stringify(out, null, 2);
  } catch (ex) {
    fb.textContent = "Score failed: " + ex.message;
  }
});

// ---------- boot ----------
(async function boot() {
  const token = localStorage.getItem(KEY_TOKEN);
  if (token) {
    try {
      const me = await api("/b2b/auth/me");
      return await enterDashboard(me);
    } catch {
      signOutLocal(); // dead token: fall through to the registration-first flow
    }
  }
  // Fresh visitors land on REGISTRATION (their shop doesn't exist yet);
  // returning merchants follow the sign-in link from there — and vice versa.
  showScreen(SCREEN_ONBOARD);
})();
