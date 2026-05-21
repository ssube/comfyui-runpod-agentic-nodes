import { app } from "../../scripts/app.js";

function candidateStrings(value, seen = new Set()) {
  if (value == null || seen.has(value)) {
    return [];
  }
  if (typeof value === "string") {
    return [value];
  }
  if (typeof value !== "object") {
    return [];
  }
  seen.add(value);
  if (Array.isArray(value)) {
    return value.flatMap((item) => candidateStrings(item, seen));
  }
  return Object.values(value).flatMap((item) => candidateStrings(item, seen));
}

function terminalFromMessage(message) {
  for (const candidate of candidateStrings(message)) {
    try {
      const payload = JSON.parse(candidate);
      const urls = payload?.terminal_urls;
      if (urls && typeof urls === "object") {
        const role = urls.agent ? "agent" : Object.keys(urls)[0];
        if (!role) {
          return null;
        }
        return { url: urls[role], auth: payload?.terminal_auth?.[role] ?? null };
      }
    } catch {
      continue;
    }
  }
  return null;
}

function terminalFrame(terminal) {
  const iframe = document.createElement("iframe");
  iframe.src = sameOriginTerminalUrl(terminal);
  iframe.title = "CRAG Web Terminal";
  iframe.style.width = "100%";
  iframe.style.height = "100%";
  iframe.style.border = "0";
  iframe.allow = "clipboard-read; clipboard-write";
  return iframe;
}

function sameOriginTerminalUrl(terminal) {
  try {
    const url = typeof terminal === "string" ? terminal : terminal.url;
    const parsed = new URL(url, window.location.href);
    if (!["127.0.0.1", "localhost"].includes(parsed.hostname) || !parsed.port) {
      return url;
    }
    const query = new URLSearchParams(parsed.search);
    const auth = typeof terminal === "string" ? null : terminal.auth;
    if (auth?.username && auth?.password) {
      query.set("__crag_terminal_auth", btoa(`${auth.username}:${auth.password}`));
    }
    const suffix = query.toString() ? `?${query}` : "";
    return `/runpod-agentic/terminal/${parsed.port}${parsed.pathname}${suffix}`;
  } catch {
    return typeof terminal === "string" ? terminal : terminal.url;
  }
}

function showFloatingTerminal(terminal) {
  let panel = document.getElementById("crag-web-terminal-panel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "crag-web-terminal-panel";
    panel.style.position = "fixed";
    panel.style.right = "16px";
    panel.style.bottom = "16px";
    panel.style.width = "min(920px, 72vw)";
    panel.style.height = "min(560px, 72vh)";
    panel.style.zIndex = "10000";
    panel.style.display = "grid";
    panel.style.gridTemplateRows = "32px 1fr";
    panel.style.background = "#111";
    panel.style.border = "1px solid #555";
    panel.style.borderRadius = "6px";
    panel.style.boxShadow = "0 18px 48px rgba(0, 0, 0, 0.45)";
    panel.style.overflow = "hidden";

    const header = document.createElement("header");
    header.style.display = "flex";
    header.style.alignItems = "center";
    header.style.justifyContent = "space-between";
    header.style.gap = "8px";
    header.style.padding = "0 8px 0 10px";
    header.style.background = "#202020";
    header.style.color = "#eee";
    header.style.font = "12px sans-serif";

    const title = document.createElement("span");
    title.textContent = "Web Terminal";
    title.style.overflow = "hidden";
    title.style.textOverflow = "ellipsis";
    title.style.whiteSpace = "nowrap";

    const actions = document.createElement("div");
    actions.style.display = "flex";
    actions.style.gap = "6px";

    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "Open";
    open.style.font = "12px sans-serif";
    open.onclick = () => window.open(panel.dataset.url, "_blank", "noopener,noreferrer");

    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.style.font = "12px sans-serif";
    close.onclick = () => panel.remove();

    actions.append(open, close);
    header.append(title, actions);
    panel.append(header);
    document.body.append(panel);
  }
  panel.dataset.url = sameOriginTerminalUrl(terminal);
  panel.querySelector("iframe")?.remove();
  panel.append(terminalFrame(terminal));
}

function attachTerminalOverlay(node, terminal) {
  if (!terminal?.url) {
    return;
  }

  if (typeof node.addWidget === "function" && !node.__cragTerminalButton) {
    node.__cragTerminalButton = node.addWidget("button", "Open Web Terminal", null, () => showFloatingTerminal(terminal));
  }

  showFloatingTerminal(terminal);
}

app.registerExtension({
  name: "comfyui-runpod-agentic.web-terminal",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!["RunLocalContainers", "RunOnRunpod"].includes(nodeData.name)) {
      return;
    }
    const original = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function onExecuted(message) {
      original?.apply(this, arguments);
      attachTerminalOverlay(this, terminalFromMessage(message));
    };
  },
});
