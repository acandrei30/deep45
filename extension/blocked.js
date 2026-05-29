const params = new URLSearchParams(window.location.search);
const blockedUrl = params.get("url") || "";
const task = params.get("task") || "";
const endTime = parseFloat(params.get("end") || "0");
const allowed = (params.get("allowed") || "").split(",").map(s => s.trim()).filter(Boolean);

document.getElementById("task").textContent = task || "(no task)";
document.getElementById("blocked-url").textContent = blockedUrl;
document.getElementById("allowed").textContent = allowed.length ? allowed.join("  ·  ") : "(nothing)";

function tick() {
  const remaining = Math.max(0, endTime - Date.now() / 1000);
  const mm = Math.floor(remaining / 60).toString().padStart(2, "0");
  const ss = Math.floor(remaining % 60).toString().padStart(2, "0");
  const t = document.getElementById("timer");
  if (remaining <= 0) {
    t.textContent = "DONE";
    if (blockedUrl) {
      setTimeout(() => { window.location.href = blockedUrl; }, 1500);
    }
    return;
  }
  t.textContent = `${mm}:${ss}`;
}

tick();
setInterval(tick, 1000);
