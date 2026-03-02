const { TelegramClient } = require("telegram");
const { NewMessage } = require("telegram/events");
const { StringSession } = require("telegram/sessions");
const input = require("input");

const apiId = parseInt(process.env.TG_API_ID || "", 10);
const apiHash = process.env.TG_API_HASH;
const stringSession = new StringSession(process.env.TG_STRING_SESSION || "");

if (!apiId || !apiHash) {
  throw new Error("Set TG_API_ID and TG_API_HASH environment variables");
}

(async () => {
  const client = new TelegramClient(stringSession, apiId, apiHash, {
    connectionRetries: 5,
  });

  await client.start({
    phoneNumber: async () => input.text("Phone number: "),
    password: async () => input.text("2FA password (if enabled): "),
    phoneCode: async () => input.text("Code: "),
    onError: (error) => console.error(error),
  });

  console.log("Logged in.");
  console.log("Session string (save for next runs):");
  console.log(client.session.save());

  client.addEventHandler((event) => {
    const msg = event.message;
    if (!msg) {
      return;
    }

    if (msg.message) {
      console.log("[text]", msg.message);
    }

    if (msg.media) {
      console.log("[media] received media");
    }
  }, new NewMessage({}));

  console.log("Listening for new messages...");
})();
