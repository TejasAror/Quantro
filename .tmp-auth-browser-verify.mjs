const DEVTOOLS = "http://localhost:9222";
const APP = "http://localhost:5173";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function getJson(url, init) {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

async function createTab() {
  const response = await fetch(`${DEVTOOLS}/json/new?${encodeURIComponent(`${APP}/login`)}`, { method: "PUT" });
  if (!response.ok) throw new Error(`create tab failed: ${response.status}`);
  return response.json();
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result ?? {});
      return;
    }
    for (const listener of listeners) listener(message);
  });
  return {
    ready: new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    }),
    on: (listener) => listeners.push(listener),
    send(method, params = {}) {
      const nextId = ++id;
      ws.send(JSON.stringify({ id: nextId, method, params }));
      return new Promise((resolve, reject) => pending.set(nextId, { resolve, reject }));
    },
    close: () => ws.close(),
  };
}

function isCandleUrl(url, symbol, interval) {
  return url.includes(`/markets/${symbol}/candles`) && url.includes(`interval=${interval}`) && url.includes("limit=240");
}

async function evalValue(cdp, expression, timeout = 8000) {
  const started = Date.now();
  let last;
  while (Date.now() - started < timeout) {
    const result = await cdp.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    last = result.result?.value;
    if (last) return last;
    await sleep(250);
  }
  return last;
}

async function waitForLoad(cdp) {
  await cdp.send("Page.navigate", { url: `${APP}/trade?chartDebug=1` });
  await cdp.send("Page.loadEventFired").catch(() => {});
  await sleep(1000);
}

async function main() {
  const version = await getJson(`${DEVTOOLS}/json/version`);
  const tab = await createTab();
  const cdp = connect(tab.webSocketDebuggerUrl);
  await cdp.ready;

  const requests = [];
  const bodies = new Map();
  const staleOrderAttempts = [];
  let pendingBodies = Promise.resolve();

  cdp.on((message) => {
    if (message.method === "Network.responseReceived") {
      const { requestId, response } = message.params;
      requests.push({ requestId, url: response.url, status: response.status, mimeType: response.mimeType });
    }
    if (message.method === "Network.loadingFinished") {
      const requestId = message.params.requestId;
      pendingBodies = pendingBodies.then(async () => {
        const request = requests.find((item) => item.requestId === requestId);
        if (!request || !request.url.includes("/markets/")) return;
        try {
          const body = await cdp.send("Network.getResponseBody", { requestId });
          bodies.set(requestId, body.body);
        } catch {
          // DevTools may discard bodies for pre-navigation requests.
        }
      });
    }
  });

  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Log.enable");

  await cdp.send("Page.navigate", { url: `${APP}/login` });
  await sleep(1000);

  await cdp.send("Runtime.evaluate", {
    expression: `localStorage.clear(); localStorage.setItem("quantro.chartDebug", "1");`,
    returnByValue: true,
  });
  const auth = await cdp.send("Runtime.evaluate", {
    awaitPromise: true,
    returnByValue: true,
    expression: `
      (async () => {
        const started = Date.now();
        while (Date.now() - started < 8000) {
          const button = Array.from(document.querySelectorAll("button"))
            .find((item) => item.textContent.trim() === "START SANDBOX ACCOUNT");
          if (button) {
            button.click();
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
        while (Date.now() - started < 12000) {
          const account = localStorage.getItem("quantro.account");
          if (account && location.pathname === "/trade") {
            return { ok: true, accountId: JSON.parse(account).id, path: location.pathname };
          }
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
        return {
          ok: false,
          path: location.pathname,
          bodyText: document.body.innerText.slice(0, 500),
          hasSandboxButton: document.body.innerText.includes("START SANDBOX ACCOUNT")
        };
      })()
    `,
  });
  if (!auth.result.value?.ok) throw new Error(`auth failed ${JSON.stringify(auth.result.value)}`);

  await waitForLoad(cdp);

  const initial = await evalValue(cdp, `(() => {
    const text = document.body.innerText;
    const canvas = document.querySelector(".lightweight-chart canvas");
    const debug = window.__quantroChartDebug || [];
    return {
      path: location.pathname,
      titleText: text.includes("BTC-USDT-SWAP"),
      unavailable: text.includes("candle feed unavailable"),
      canvasCount: document.querySelectorAll(".lightweight-chart canvas").length,
      canvasSize: canvas ? { width: canvas.width, height: canvas.height } : null,
      chartStatus: Array.from(document.querySelectorAll(".chart-status span")).map((n) => n.textContent),
      debugTail: debug.slice(-20),
    };
  })()`, 12000);

  await sleep(1500);
  await pendingBodies;

  async function switchInterval(interval) {
    await cdp.send("Runtime.evaluate", {
      expression: `
        (() => {
          const buttons = Array.from(document.querySelectorAll(".timeframes button"));
          const button = buttons.find((item) => item.textContent.trim() === ${JSON.stringify(interval)});
          if (!button) return false;
          button.click();
          return true;
        })()
      `,
      returnByValue: true,
    });
    await sleep(1400);
    await pendingBodies;
    return cdp.send("Runtime.evaluate", {
      returnByValue: true,
      expression: `
        (() => {
          const debug = window.__quantroChartDebug || [];
          return {
            interval: ${JSON.stringify(interval)},
            unavailable: document.body.innerText.includes("candle feed unavailable"),
            status: Array.from(document.querySelectorAll(".chart-status span")).map((n) => n.textContent),
            resolved: debug.filter((e) => e.name === "api.candles:resolved" && e.payload.interval === ${JSON.stringify(interval)}).slice(-1)[0] || null,
            setData: debug.filter((e) => e.name === "chart:setData" && e.payload.count > 0).slice(-1)[0] || null,
          };
        })()
      `,
    }).then((r) => r.result.value);
  }

  const intervals = [];
  for (const interval of ["1m", "5m", "15m", "1h", "4h", "1d"]) {
    intervals.push(await switchInterval(interval));
  }

  await cdp.send("Runtime.evaluate", {
    expression: `
      (() => {
        const buttons = Array.from(document.querySelectorAll("button"));
        const eth = buttons.find((item) => item.innerText.includes("ETH-USDT-SWAP"));
        if (eth) eth.click();
        return Boolean(eth);
      })()
    `,
    returnByValue: true,
  });
  await sleep(2000);
  await pendingBodies;
  const ethState = await evalValue(cdp, `(() => {
    const debug = window.__quantroChartDebug || [];
    return {
      hasEth: document.body.innerText.includes("ETH-USDT-SWAP"),
      unavailable: document.body.innerText.includes("candle feed unavailable"),
      resolved: debug.filter((e) => e.name === "api.candles:resolved" && e.payload.symbol === "ETH-USDT-SWAP").slice(-1)[0] || null,
      setData: debug.filter((e) => e.name === "chart:setData" && e.payload.count > 0).slice(-1)[0] || null,
    };
  })()`, 8000);

  await waitForLoad(cdp);
  await sleep(2500);
  await pendingBodies;
  const refreshed = await evalValue(cdp, `(() => {
    const text = document.body.innerText;
    const debug = window.__quantroChartDebug || [];
    return {
      path: location.pathname,
      unavailable: text.includes("candle feed unavailable"),
      hasBtc: text.includes("BTC-USDT-SWAP"),
      resolved: debug.filter((e) => e.name === "api.candles:resolved").slice(-1)[0] || null,
      setData: debug.filter((e) => e.name === "chart:setData" && e.payload.count > 0).slice(-1)[0] || null,
    };
  })()`, 10000);

  await sleep(6000);
  await pendingBodies;
  const afterUpdates = await evalValue(cdp, `(() => ({
    unavailable: document.body.innerText.includes("candle feed unavailable"),
    tickerFunctional: /Last|24h|Mark|Index|BTC-USDT-SWAP/.test(document.body.innerText),
    orderBookFunctional: document.body.innerText.includes("Order Book") || document.body.innerText.includes("ORDER BOOK"),
    buttonCount: document.querySelectorAll("button").length,
  }))()`, 3000);

  const marketRequests = requests.filter((item) => item.url.includes("/markets/"));
  const directBackend = requests.filter((item) => item.url.includes("127.0.0.1:8000"));
  const candleResponses = marketRequests
    .filter((item) => item.url.includes("/candles"))
    .map((item) => {
      let parsed = null;
      const body = bodies.get(item.requestId);
      if (body) {
        try {
          parsed = JSON.parse(body);
        } catch {
          parsed = null;
        }
      }
      return {
        url: item.url,
        status: item.status,
        candles: Array.isArray(parsed?.candles) ? parsed.candles.length : null,
        symbol: parsed?.symbol ?? null,
        interval: parsed?.interval ?? null,
      };
    });

  const exact1h = candleResponses.filter((item) => isCandleUrl(item.url, "BTC-USDT-SWAP", "1h")).slice(-1)[0] || null;
  const tickerResponses = marketRequests.filter((item) => item.url.includes("/ticker")).map((item) => ({ url: item.url, status: item.status })).slice(-5);
  const orderBookResponses = marketRequests.filter((item) => item.url.includes("/orderbook")).map((item) => ({ url: item.url, status: item.status })).slice(-5);

  const staleProbe = await cdp.send("Runtime.evaluate", {
    awaitPromise: true,
    returnByValue: true,
    expression: `
      (async () => {
        const account = JSON.parse(localStorage.getItem("quantro.account") || "null");
        const token = localStorage.getItem("quantro.accessToken");
        if (!account || !token) return { skipped: "missing authenticated account" };
        const response = await fetch("/orders", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
          body: JSON.stringify({
            account_id: account.id,
            symbol: "NO-MARKET-DATA-SWAP",
            side: "buy",
            order_type: "market",
            time_in_force: "gtc",
            quantity: "1",
            price: "0",
            stop_price: "0"
          })
        });
        let body = null;
        try { body = await response.json(); } catch {}
        return { status: response.status, body };
      })()
    `,
  });
  staleOrderAttempts.push(staleProbe.result.value);

  console.log(JSON.stringify({
    chrome: version.Browser,
    auth: auth.result.value,
    initial,
    exact1h,
    candleResponses,
    intervals,
    ethState,
    refreshed,
    afterUpdates,
    tickerResponses,
    orderBookResponses,
    directBackendRequestCount: directBackend.length,
    directBackendRequests: directBackend.map((item) => item.url),
    staleOrderAttempts,
  }, null, 2));

  cdp.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
