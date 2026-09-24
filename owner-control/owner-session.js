/* Owner lease UI only. The full vendor noVNC application remains unchanged. */
(() => {
  "use strict";
  document.documentElement.style.visibility = "hidden";
  const bar = document.createElement("div");
  bar.id = "phone-owner-session";
  Object.assign(bar.style, {
    position: "fixed", top: "0", right: "0", zIndex: "2147483647",
    padding: "8px", background: "#18212b", color: "white", font: "14px sans-serif"
  });
  const label = document.createElement("span");
  const link = document.createElement("a");
  link.href = "/owner";
  link.textContent = " Управление / завершить";
  link.style.color = "#8bd0ff";
  bar.append(label, link);
  document.body.append(bar);
  let leaving = false;
  let uiLoaded = false;
  const leave = () => {
    leaving = true;
    document.documentElement.style.visibility = "hidden";
    window.location.replace("/owner");
  };
  const check = async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      const response = await fetch("/owner/status", {
        cache: "no-store", credentials: "same-origin", redirect: "error",
        signal: controller.signal
      });
      if (!response.ok) throw new Error("status unavailable");
      const state = await response.json();
      if (state.active !== true || !Number.isInteger(state.remaining) ||
          state.remaining < 1 || state.remaining > 900) {
        leave();
        return;
      }
      if (!uiLoaded) {
        await import("/app/ui.js");
        uiLoaded = true;
      }
      const minutes = Math.floor(state.remaining / 60);
      const seconds = String(state.remaining % 60).padStart(2, "0");
      label.textContent = `Управление активно · осталось ${minutes}:${seconds} ·`;
      document.documentElement.style.visibility = "visible";
    } catch {
      leave();
    } finally {
      clearTimeout(timeout);
      if (!leaving) setTimeout(check, 5000);
    }
  };
  void check();
})();
