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

function shouldDispatch(update, env) {
  const actorId = actorIdFromUpdate(update);
  const allowedUserId = String(env.TELEGRAM_ALLOWED_USER_ID || "").trim();
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
    return looksRelevantDirectText(text);
  }

  const allowedGroupChatIds = csvSet(env.ALLOWED_GROUP_CHAT_IDS);
  if (!allowedGroupChatIds.has(chatId)) {
    return false;
  }
  return text.startsWith("/") || hasBotMention(text, env.BOT_USERNAME);
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

async function dispatchGitHubInputs(inputs, env) {
  const { repo, workflowFile, ref, token } = githubDispatchConfig(env);
  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "telegram-github-dispatcher-worker",
        "x-github-api-version": "2022-11-28",
      },
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

async function dispatchTelegramUpdate(update, env) {
  const updateB64 = base64Utf8(JSON.stringify(update));
  await dispatchGitHubInputs(
    {
      task: "telegram-update",
      update_b64: updateB64,
      source: "cloudflare-worker",
    },
    env,
  );
}

function scheduledInputsFromCron(cron, scheduledTime) {
  switch (String(cron || "").trim()) {
    case "*/30 * * * *":
      return {
        task: "pancake-td-sync",
        pancake_notify: "auto",
        source: "cloudflare-cron",
      };
    case "0 1 * * *":
      return {
        task: "daily-report",
        slot: "morning",
        source: "cloudflare-cron",
      };
    case "0 2 * * *":
      return {
        task: "token-health",
        source: "cloudflare-cron",
      };
    case "0 8 * * 1,5,6": {
      const dayOfWeek = new Date(scheduledTime || Date.now()).getUTCDay();
      if (dayOfWeek === 6) {
        return {
          task: "reconcile-weekly",
          source: "cloudflare-cron",
        };
      }
      return {
        task: "reconcile-cash-in",
        source: "cloudflare-cron",
      };
    }
    case "0 14 * * *":
      return {
        task: "daily-report",
        slot: "evening",
        source: "cloudflare-cron",
      };
    default:
      return null;
  }
}

async function sendAck(update, env) {
  if (String(env.CLOUD_DISPATCH_ACK_ENABLED || "0").trim() !== "1") {
    return;
  }
  const token = String(env.TELEGRAM_BOT_TOKEN || "").trim();
  const chatId = chatIdFromUpdate(update);
  if (!token || !chatId || chatTypeFromUpdate(update) !== "private") {
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
    if (request.method !== "POST" || url.pathname !== "/telegram/webhook") {
      return jsonResponse({ ok: false, error: "not_found" }, 404);
    }

    const expectedSecret = String(env.TELEGRAM_WEBHOOK_SECRET || "").trim();
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

    if (!shouldDispatch(update, env)) {
      return jsonResponse({ ok: true, dispatched: false });
    }

    try {
      await dispatchTelegramUpdate(update, env);
      ctx.waitUntil(sendAck(update, env));
      return jsonResponse({ ok: true, dispatched: true });
    } catch (error) {
      console.error(error);
      return jsonResponse({ ok: false, error: "dispatch_failed" }, 500);
    }
  },

  async scheduled(event, env) {
    const inputs = scheduledInputsFromCron(event.cron, event.scheduledTime);
    if (!inputs) {
      console.log(`No GitHub task mapped for cron: ${event.cron}`);
      return;
    }
    await dispatchGitHubInputs(inputs, env);
    console.log(`Dispatched GitHub task ${inputs.task} from cron ${event.cron}`);
  },
};
