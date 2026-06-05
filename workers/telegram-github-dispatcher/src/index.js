const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function textFromUpdate(update) {
  const message = update.message || update.edited_message || update.channel_post || update.edited_channel_post;
  if (!message) return "";
  return String(message.text || message.caption || "").trim();
}

function messageFromUpdate(update) {
  return update.message || update.edited_message || update.channel_post || update.edited_channel_post || null;
}

function actorIdFromUpdate(update) {
  if (update.callback_query && update.callback_query.from) {
    return String(update.callback_query.from.id || "");
  }
  const message = messageFromUpdate(update);
  if (message && message.from) {
    return String(message.from.id || "");
  }
  return "";
}

function chatIdFromUpdate(update) {
  if (update.callback_query && update.callback_query.message && update.callback_query.message.chat) {
    return String(update.callback_query.message.chat.id || "");
  }
  const message = messageFromUpdate(update);
  if (message && message.chat) {
    return String(message.chat.id || "");
  }
  return "";
}

function chatTypeFromUpdate(update) {
  if (update.callback_query && update.callback_query.message && update.callback_query.message.chat) {
    return String(update.callback_query.message.chat.type || "");
  }
  const message = messageFromUpdate(update);
  if (message && message.chat) {
    return String(message.chat.type || "");
  }
  return "";
}

function csvSet(value) {
  return new Set(
    String(value || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  );
}

function hasBotMention(text, botUsername) {
  const username = String(botUsername || "").trim().replace(/^@/, "").toLowerCase();
  if (!username) return false;
  return text.toLowerCase().includes(`@${username}`);
}

function looksRelevantDirectText(text) {
  const lower = text.toLowerCase();
  return (
    text.startsWith("/") ||
    lower.includes("facebook.com") ||
    lower.includes("fb.watch") ||
    lower.includes("lên") ||
    lower.includes("len ") ||
    lower.includes("báo cáo") ||
    lower.includes("bao cao") ||
    lower.includes("đối soát") ||
    lower.includes("doi soat") ||
    lower.includes("lên đơn") ||
    lower.includes("len don") ||
    lower.includes("token")
  );
}

function shouldDispatch(update, env, botName = "main") {
  const isBot3 = String(botName || "").trim().toLowerCase() === "bot3";
  const actorId = actorIdFromUpdate(update);
  const allowedUserId = String(
    isBot3 ? env.BOT3_ALLOWED_USER_ID || env.TELEGRAM_ALLOWED_USER_ID || "" : env.TELEGRAM_ALLOWED_USER_ID || "",
  ).trim();
  if (allowedUserId && actorId && actorId !== allowedUserId) {
    return false;
  }

  if (update.callback_query) {
    return Boolean(actorId);
  }

  const message = messageFromUpdate(update);
  if (!message) {
    return false;
  }
  if (Array.isArray(message.new_chat_members) && message.new_chat_members.length > 0) {
    return true;
  }

  const chatType = chatTypeFromUpdate(update);
  const chatId = chatIdFromUpdate(update);
  const text = textFromUpdate(update);
  if (!text) {
    return false;
  }

  if (chatType === "private") {
    if (isBot3) {
      return true;
    }
    return looksRelevantDirectText(text);
  }

  const allowedGroupChatIds = csvSet(
    isBot3 ? env.BOT3_ALLOWED_GROUP_CHAT_IDS || env.BOT3_TASK_GROUP_CHAT_ID || "" : env.ALLOWED_GROUP_CHAT_IDS,
  );
  if (!allowedGroupChatIds.has(chatId)) {
    return false;
  }
  return text.startsWith("/") || hasBotMention(text, isBot3 ? env.BOT3_USERNAME : env.BOT_USERNAME);
}

function base64Utf8(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary);
}

function githubDispatchConfig(env) {
  const repo = String(env.GITHUB_REPO || "").trim();
  const workflowFile = String(env.GITHUB_WORKFLOW_FILE || "free-scheduled-tasks.yml").trim();
  const ref = String(env.GITHUB_REF || "main").trim();
  const token = String(env.GITHUB_TOKEN || "").trim();
  if (!repo || !workflowFile || !ref || !token) {
    throw new Error("Missing GitHub dispatcher configuration.");
  }
  return { repo, workflowFile, ref, token };
}

function githubHeaders(token) {
  return {
    authorization: `Bearer ${token}`,
    accept: "application/vnd.github+json",
    "content-type": "application/json",
    "user-agent": "telegram-github-dispatcher-worker",
    "x-github-api-version": "2022-11-28",
  };
}

async function dispatchGitHubInputs(inputs, env) {
  const { repo, workflowFile, ref, token } = githubDispatchConfig(env);
  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: githubHeaders(token),
      body: JSON.stringify({
        ref,
        inputs,
      }),
    },
  );
  if (response.status !== 204) {
    const body = await response.text();
    throw new Error(`GitHub dispatch failed: ${response.status} ${body.slice(0, 500)}`);
  }
}

async function dispatchTelegramUpdate(update, env, botName = "main") {
  const updateB64 = base64Utf8(JSON.stringify(update));
  await dispatchGitHubInputs(
    {
      task: "telegram-update",
      bot: String(botName || "main").trim().toLowerCase() === "bot3" ? "bot3" : "main",
      update_b64: updateB64,
      source: "cloudflare-worker",
    },
    env,
  );
}

function scheduleGuardEnabled(env) {
  return String(env.SCHEDULE_GUARD_ENABLED || "1").trim() !== "0";
}

function scheduleGuardSecret(env) {
  return String(env.SCHEDULE_GUARD_SECRET || env.TELEGRAM_WEBHOOK_SECRET || "").trim();
}

function scheduleGuardVariableName(env) {
  return String(env.SCHEDULE_GUARD_VARIABLE_NAME || "LOCAL_SCHEDULE_MARKS").trim();
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function localDateParts(timestamp) {
  const local = new Date(Number(timestamp || Date.now()) + 7 * 60 * 60 * 1000);
  return {
    year: local.getUTCFullYear(),
    month: local.getUTCMonth() + 1,
    day: local.getUTCDate(),
    hour: local.getUTCHours(),
    minute: local.getUTCMinutes(),
  };
}

function localRunDate(timestamp) {
  const parts = localDateParts(timestamp);
  return `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}`;
}

function localHalfHourBucket(timestamp) {
  const parts = localDateParts(timestamp);
  const minute = parts.minute >= 30 ? 30 : 0;
  return `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}T${pad2(parts.hour)}:${pad2(minute)}`;
}

function scheduleMarkKey(parts) {
  const task = String(parts.task || "").trim();
  if (!task) return "";
  if (task === "daily-report" || task === "bot3-daily-checkin") {
    const slot = String(parts.slot || "").trim();
    const runDate = String(parts.run_date || "").trim();
    return slot && runDate ? `${task}:${slot}:${runDate}` : "";
  }
  if (task === "pancake-td-sync") {
    const bucket = String(parts.bucket || "").trim();
    return bucket ? `${task}:${bucket}` : "";
  }
  const runDate = String(parts.run_date || "").trim();
  return runDate ? `${task}:${runDate}` : "";
}

function scheduleMarkPartsFromCloud(inputs, scheduledTime) {
  const task = String(inputs.task || "").trim();
  if (task === "daily-report" || task === "bot3-daily-checkin") {
    return {
      task,
      slot: String(inputs.slot || "").trim(),
      run_date: localRunDate(scheduledTime),
    };
  }
  if (task === "pancake-td-sync") {
    return {
      task,
      bucket: localHalfHourBucket(scheduledTime),
    };
  }
  return {
    task,
    run_date: localRunDate(scheduledTime),
  };
}

function normalizeScheduleMarks(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return { version: 1, marks: {} };
  }
  const marks = payload.marks && typeof payload.marks === "object" && !Array.isArray(payload.marks) ? payload.marks : {};
  return { version: 1, marks };
}

async function loadScheduleMarks(env) {
  const { repo, token } = githubDispatchConfig(env);
  const variableName = scheduleGuardVariableName(env);
  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/variables/${encodeURIComponent(variableName)}`,
    {
      method: "GET",
      headers: githubHeaders(token),
    },
  );
  if (response.status === 404) {
    return { exists: false, payload: { version: 1, marks: {} } };
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub variable read failed: ${response.status} ${body.slice(0, 300)}`);
  }
  const data = await response.json();
  let payload = { version: 1, marks: {} };
  try {
    payload = normalizeScheduleMarks(JSON.parse(String(data.value || "{}")));
  } catch (_error) {
    payload = { version: 1, marks: {} };
  }
  return { exists: true, payload };
}

function pruneScheduleMarks(payload, maxMarks = 200) {
  const entries = Object.entries(payload.marks || {});
  if (entries.length <= maxMarks) return payload;
  entries.sort((left, right) => String(left[1]?.marked_at || "").localeCompare(String(right[1]?.marked_at || "")));
  return {
    version: 1,
    marks: Object.fromEntries(entries.slice(-maxMarks)),
  };
}

async function saveScheduleMarks(env, payload, exists) {
  const { repo, token } = githubDispatchConfig(env);
  const variableName = scheduleGuardVariableName(env);
  const value = JSON.stringify(pruneScheduleMarks(payload));
  const url = exists
    ? `https://api.github.com/repos/${repo}/actions/variables/${encodeURIComponent(variableName)}`
    : `https://api.github.com/repos/${repo}/actions/variables`;
  const body = exists ? { name: variableName, value } : { name: variableName, value };
  const response = await fetch(url, {
    method: exists ? "PATCH" : "POST",
    headers: githubHeaders(token),
    body: JSON.stringify(body),
  });
  if (!response.ok && !(exists && response.status === 204)) {
    const text = await response.text();
    throw new Error(`GitHub variable write failed: ${response.status} ${text.slice(0, 300)}`);
  }
}

async function hasLocalCompletionMark(inputs, scheduledTime, env) {
  if (!scheduleGuardEnabled(env)) return false;
  const parts = scheduleMarkPartsFromCloud(inputs, scheduledTime);
  const key = scheduleMarkKey(parts);
  if (!key) return false;
  try {
    const { payload } = await loadScheduleMarks(env);
    return Boolean(payload.marks && payload.marks[key]);
  } catch (error) {
    console.error(`Schedule guard read failed for ${key}:`, error);
    return false;
  }
}

async function handleScheduleMark(request, env) {
  if (!scheduleGuardEnabled(env)) {
    return jsonResponse({ ok: false, error: "disabled" }, 403);
  }
  const expectedSecret = scheduleGuardSecret(env);
  const providedSecret = request.headers.get("X-Schedule-Guard-Secret") || "";
  if (!expectedSecret || providedSecret !== expectedSecret) {
    return jsonResponse({ ok: false, error: "forbidden" }, 403);
  }
  let body;
  try {
    body = await request.json();
  } catch (_error) {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const parts = {
    task: String(body.task || "").trim(),
    slot: String(body.slot || "").trim(),
    run_date: String(body.run_date || "").trim(),
    bucket: String(body.bucket || "").trim(),
  };
  const key = scheduleMarkKey(parts);
  if (!key) {
    return jsonResponse({ ok: false, error: "invalid_marker" }, 400);
  }
  try {
    const { exists, payload } = await loadScheduleMarks(env);
    payload.marks[key] = {
      ...parts,
      source: String(body.source || "local").trim() || "local",
      marked_at: new Date().toISOString(),
    };
    await saveScheduleMarks(env, payload, exists);
    return jsonResponse({ ok: true, key });
  } catch (error) {
    console.error(`Schedule guard mark failed for ${key}:`, error);
    return jsonResponse({ ok: false, error: "mark_failed" }, 500);
  }
}

function scheduledInputsFromCron(cron, scheduledTime) {
  const parts = localDateParts(scheduledTime);
  switch (String(cron || "").trim()) {
    case "5,35 * * * *": {
      const inputs = [{
        task: "pancake-td-sync",
        pancake_notify: "auto",
        source: "cloudflare-cron",
      }];
      if (parts.hour === 17 && parts.minute === 5) {
        inputs.push({
          task: "bot3-daily-checkin",
          slot: "evening",
          source: "cloudflare-cron",
        });
      }
      return inputs;
    }
    case "5 1 * * *":
      return [{
        task: "daily-report",
        slot: "morning",
        source: "cloudflare-cron",
      }];
    case "5 2 * * *":
      return [
        {
          task: "token-health",
          source: "cloudflare-cron",
        },
        {
          task: "bot3-daily-checkin",
          slot: "morning",
          source: "cloudflare-cron",
        },
      ];
    case "5 8 * * 1,5,6": {
      const dayOfWeek = new Date(scheduledTime || Date.now()).getUTCDay();
      if (dayOfWeek === 6) {
        return [{
          task: "reconcile-weekly",
          source: "cloudflare-cron",
        }];
      }
      return [{
        task: "reconcile-cash-in",
        source: "cloudflare-cron",
      }];
    }
    case "5 14 * * *":
      return [{
        task: "daily-report",
        slot: "evening",
        source: "cloudflare-cron",
      }];
    default:
      return [];
  }
}

async function sendAck(update, env, botName = "main") {
  if (String(env.CLOUD_DISPATCH_ACK_ENABLED || "0").trim() !== "1") {
    return;
  }
  const isBot3 = String(botName || "").trim().toLowerCase() === "bot3";
  const token = String(isBot3 ? env.BOT3_TELEGRAM_TOKEN || "" : env.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) {
    return;
  }
  const callbackId = String((update.callback_query && update.callback_query.id) || "").trim();
  if (callbackId) {
    await fetch(`https://api.telegram.org/bot${token}/answerCallbackQuery`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        callback_query_id: callbackId,
        text: "Cloud đang xử lý, anh chờ em một chút.",
        show_alert: false,
      }),
    });
  }
  const chatId = chatIdFromUpdate(update);
  if (!chatId || chatTypeFromUpdate(update) !== "private") {
    return;
  }
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text: "Em nhận lệnh rồi. Cloud đang xử lý, anh chờ em một chút.",
    }),
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") {
      return jsonResponse({ ok: true, service: "telegram-github-dispatcher" });
    }
    if (request.method === "POST" && url.pathname === "/schedule/mark") {
      return handleScheduleMark(request, env);
    }
    const botName = url.pathname === "/telegram/webhook/bot3" ? "bot3" : "main";
    if (request.method !== "POST" || !["/telegram/webhook", "/telegram/webhook/bot3"].includes(url.pathname)) {
      return jsonResponse({ ok: false, error: "not_found" }, 404);
    }

    const expectedSecret = String(
      botName === "bot3"
        ? env.BOT3_TELEGRAM_WEBHOOK_SECRET || env.TELEGRAM_WEBHOOK_SECRET || ""
        : env.TELEGRAM_WEBHOOK_SECRET || "",
    ).trim();
    const providedSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
    if (!expectedSecret || providedSecret !== expectedSecret) {
      return jsonResponse({ ok: false, error: "forbidden" }, 403);
    }

    let update;
    try {
      update = await request.json();
    } catch (_error) {
      return jsonResponse({ ok: false, error: "invalid_json" }, 400);
    }

    if (!shouldDispatch(update, env, botName)) {
      return jsonResponse({ ok: true, dispatched: false });
    }

    try {
      await dispatchTelegramUpdate(update, env, botName);
      ctx.waitUntil(sendAck(update, env, botName));
      return jsonResponse({ ok: true, dispatched: true });
    } catch (error) {
      console.error(error);
      return jsonResponse({ ok: false, error: "dispatch_failed" }, 500);
    }
  },

  async scheduled(event, env) {
    const inputList = scheduledInputsFromCron(event.cron, event.scheduledTime);
    if (!inputList.length) {
      console.log(`No GitHub task mapped for cron: ${event.cron}`);
      return;
    }
    for (const inputs of inputList) {
      if (await hasLocalCompletionMark(inputs, event.scheduledTime, env)) {
        console.log(`Skipped GitHub task ${inputs.task} from cron ${event.cron}; local completion mark exists.`);
        continue;
      }
      await dispatchGitHubInputs(inputs, env);
      console.log(`Dispatched GitHub task ${inputs.task} from cron ${event.cron}`);
    }
  },
};
