/**
 * Cloudflare Email Worker
 *
 * Receives emails via Cloudflare Email Routing, extracts the content,
 * and POSTs it as JSON to the Hypersecretary webhook.
 *
 * Setup:
 *   1. npm install postal-mime
 *   2. npx wrangler secret put WEBHOOK_URL
 *      → https://hypersecretary.fly.dev/webhook/email
 *   3. npx wrangler secret put WEBHOOK_SECRET
 *      → (same value as WEBHOOK_SECRET in your bot's .env)
 *   4. npx wrangler deploy
 *   5. In Cloudflare dashboard → Email Routing → Create route:
 *      hypersecretary@markwatson.ai → Email Worker → this worker
 */

import PostalMime from "postal-mime";

export default {
  async email(message, env, ctx) {
    try {
      // Read the raw email
      const rawEmail = await new Response(message.raw).arrayBuffer();

      // Parse with PostalMime
      const parser = new PostalMime();
      const parsed = await parser.parse(rawEmail);

      // Extract plain text body, falling back to stripped HTML
      let body = parsed.text || "";
      if (!body && parsed.html) {
        // Basic HTML stripping — good enough for most emails
        body = parsed.html
          .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
          .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
          .replace(/<[^>]+>/g, " ")
          .replace(/&nbsp;/g, " ")
          .replace(/&amp;/g, "&")
          .replace(/&lt;/g, "<")
          .replace(/&gt;/g, ">")
          .replace(/\s+/g, " ")
          .trim();
      }

      // Truncate very long emails (saves tokens later)
      if (body.length > 10000) {
        body = body.substring(0, 10000) + "\n\n[... truncated]";
      }

      const payload = {
        from: message.from,
        to: message.to,
        subject: message.headers.get("subject") || "(no subject)",
        body: body,
        message_id: message.headers.get("message-id") || "",
        date: message.headers.get("date") || new Date().toISOString(),
      };

      const response = await fetch(env.WEBHOOK_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Webhook-Secret": env.WEBHOOK_SECRET,
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        console.error(`Webhook failed: ${response.status} ${await response.text()}`);
        // Don't reject — we don't want Cloudflare to retry and flood
      }
    } catch (error) {
      console.error("Email processing error:", error);
    }
  },
};
