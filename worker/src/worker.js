import { EmailMessage } from "cloudflare:email";

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

    if (!env.SEND_EMAIL || typeof env.SEND_EMAIL.send !== "function") {
      return json({ ok: false, error: "missing_send_email_binding" }, 500);
    }

    const sent = [];
    for (const recipient of mail.to) {
      const rawMessage = buildMimeMessage(mail, recipient);
      const emailMessage = new EmailMessage(mail.from, recipient, rawMessage);
      try {
        await env.SEND_EMAIL.send(emailMessage);
        sent.push(recipient);
      } catch (error) {
        return json(
          {
            ok: false,
            error: "cloudflare_send_email_failed",
            recipient,
            message: error instanceof Error ? error.message : String(error),
          },
          502,
        );
      }
    }

    return json({ ok: true, sent });
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
  const replyTo = firstHeaderAddress(message.replyTo);
  const fromHeader = env.FROM_EMAIL || message.from || envelope.from;
  const from = firstHeaderAddress(fromHeader) || firstHeaderAddress(envelope.from);
  const text = message.text || (message.html ? stripHtml(message.html) : "");
  const html = message.html || (text ? textToHtml(text) : "");

  const mail = {
    from,
    fromHeader,
    to,
    subject: message.subject || env.DEFAULT_SUBJECT || "Verification code",
    text,
    html,
  };

  if (replyTo) {
    mail.replyTo = replyTo;
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

function buildMimeMessage(mail, recipient) {
  const boundary = `smtp2worker-${crypto.randomUUID()}`;
  const headers = [
    `From: ${formatAddressHeader(mail.fromHeader, mail.from)}`,
    `To: ${formatAddressHeader(recipient, recipient)}`,
    `Subject: ${encodeHeaderValue(mail.subject)}`,
    `Date: ${new Date().toUTCString()}`,
    `Message-ID: <${crypto.randomUUID()}@smtp2worker.local>`,
    "MIME-Version: 1.0",
  ];

  if (mail.replyTo) {
    headers.push(`Reply-To: ${formatAddressHeader(mail.replyTo, mail.replyTo)}`);
  }

  if (mail.text && mail.html) {
    return [
      ...headers,
      `Content-Type: multipart/alternative; boundary="${boundary}"`,
      "",
      `--${boundary}`,
      mimePart("text/plain", mail.text),
      `--${boundary}`,
      mimePart("text/html", mail.html),
      `--${boundary}--`,
      "",
    ].join("\r\n");
  }

  return [
    ...headers,
    mail.html ? mimePart("text/html", mail.html) : mimePart("text/plain", mail.text),
    "",
  ].join("\r\n");
}

function mimePart(contentType, content) {
  return [
    `Content-Type: ${contentType}; charset=UTF-8`,
    "Content-Transfer-Encoding: base64",
    "",
    wrapBase64(base64Utf8(content)),
  ].join("\r\n");
}

function formatAddressHeader(value, fallbackAddress) {
  const text = sanitizeHeaderValue(value);
  const angleMatch = text.match(/^(.*?)<([^<>@\s]+@[^<>\s]+)>$/);
  if (angleMatch) {
    const name = angleMatch[1].trim().replace(/^"|"$/g, "");
    const address = angleMatch[2].trim();
    return name ? `${encodeHeaderValue(name)} <${address}>` : address;
  }
  const address = firstHeaderAddress(text) || fallbackAddress;
  return sanitizeHeaderValue(address);
}

function encodeHeaderValue(value) {
  const text = sanitizeHeaderValue(value);
  if (/^[\x20-\x7e]*$/.test(text)) {
    return text;
  }
  return `=?UTF-8?B?${base64Utf8(text)}?=`;
}

function sanitizeHeaderValue(value) {
  return String(value || "")
    .replace(/[\r\n]+/g, " ")
    .trim();
}

function base64Utf8(value) {
  const bytes = new TextEncoder().encode(String(value || ""));
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function wrapBase64(value) {
  return String(value).replace(/.{1,76}/g, "$&\r\n").trimEnd();
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
