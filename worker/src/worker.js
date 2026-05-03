const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
};

export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return json({ ok: true, service: "smtp2worker" });
    }

    if (request.method !== "POST") {
      return json({ ok: false, error: "method_not_allowed" }, 405);
    }

    if (!isAuthorized(request, env)) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ ok: false, error: "invalid_json" }, 400);
    }

    const mail = normalizeMail(payload, env);
    const validationError = validateMail(mail, env);
    if (validationError) {
      return json({ ok: false, error: validationError }, 400);
    }

    if (env.DRY_RUN === "true") {
      return json({ ok: true, dryRun: true, to: mail.to, subject: mail.subject });
    }

    if (!env.RESEND_API_KEY) {
      return json({ ok: false, error: "missing_resend_api_key" }, 500);
    }

    const resendResponse = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        "authorization": `Bearer ${env.RESEND_API_KEY}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(mail),
    });

    const responseText = await resendResponse.text();
    let responseBody;
    try {
      responseBody = responseText ? JSON.parse(responseText) : {};
    } catch {
      responseBody = { raw: responseText };
    }

    if (!resendResponse.ok) {
      return json(
        {
          ok: false,
          error: "mail_provider_rejected",
          providerStatus: resendResponse.status,
          provider: responseBody,
        },
        resendResponse.status >= 400 && resendResponse.status < 500 ? 400 : 502,
      );
    }

    return json({ ok: true, provider: responseBody });
  },
};

function isAuthorized(request, env) {
  if (!env.BRIDGE_TOKEN) {
    return false;
  }
  const authorization = request.headers.get("authorization") || "";
  const headerToken = request.headers.get("x-smtp2worker-token") || "";
  const legacyHeaderToken = request.headers.get("x-smtp2http-token") || "";
  return (
    authorization === `Bearer ${env.BRIDGE_TOKEN}` ||
    headerToken === env.BRIDGE_TOKEN ||
    legacyHeaderToken === env.BRIDGE_TOKEN
  );
}

function normalizeMail(payload, env) {
  const envelope = payload?.envelope || {};
  const message = payload?.message || {};
  const to = normalizeRecipients(envelope.to || message.to);
  const cc = normalizeRecipients(message.cc);
  const replyTo = firstHeaderAddress(message.replyTo);
  const from = env.FROM_EMAIL || firstHeaderAddress(message.from) || envelope.from;
  const text = message.text || (message.html ? stripHtml(message.html) : "");
  const html = message.html || (text ? textToHtml(text) : "");

  const mail = {
    from,
    to,
    subject: message.subject || env.DEFAULT_SUBJECT || "Verification code",
    text,
    html,
  };

  if (cc.length > 0) {
    mail.cc = cc;
  }
  if (replyTo) {
    mail.reply_to = replyTo;
  }

  return mail;
}

function validateMail(mail, env) {
  if (!mail.from) {
    return "missing_from";
  }
  if (!mail.to || mail.to.length === 0) {
    return "missing_recipient";
  }
  if (!mail.subject) {
    return "missing_subject";
  }
  if (!mail.text && !mail.html) {
    return "missing_body";
  }

  const allowList = splitList(env.ALLOWED_RECIPIENT_DOMAINS);
  if (allowList.length === 0) {
    return "";
  }

  const blocked = mail.to.find((address) => {
    const domain = address.split("@").pop()?.toLowerCase();
    return !domain || !allowList.includes(domain);
  });
  return blocked ? "recipient_domain_not_allowed" : "";
}

function normalizeRecipients(value) {
  if (!value) {
    return [];
  }
  const values = Array.isArray(value) ? value : String(value).split(",");
  return values
    .flatMap((item) => String(item).split(","))
    .map(firstHeaderAddress)
    .filter(Boolean);
}

function firstHeaderAddress(value) {
  if (!value) {
    return "";
  }
  const text = String(value).trim();
  const angleMatch = text.match(/<([^<>@\s]+@[^<>\s]+)>/);
  if (angleMatch) {
    return angleMatch[1].trim();
  }
  const directMatch = text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
  return directMatch ? directMatch[0].trim() : "";
}

function textToHtml(text) {
  return escapeHtml(text).replace(/\r?\n/g, "<br>");
}

function stripHtml(html) {
  return String(html)
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function splitList(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: JSON_HEADERS,
  });
}
