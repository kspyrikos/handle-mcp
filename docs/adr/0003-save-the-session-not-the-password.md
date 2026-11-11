# 0003: Save the session, not the password

`handle-mcp login <store>` uses the password for one login and forgets it. It saves only what the store gives back after a login:

- nemlig.com: the `.ASPXAUTH` cookie (lives 1 year).
- BilkaToGo: the Gigya `login_token`, which buys a fresh 24-hour JWT on each start.

The keychain entry (and the `--from-env` file) is `{"email", "session"}`. When the session dies, the tool says "The login for <store> has expired. Run in a terminal: handle-mcp login <store>".

## Why

On macOS any process running as the user can read a keychain item made by the `security` CLI, without a prompt (reported in #81). A stolen password is often reused elsewhere and cannot be revoked. A stolen session works only at one store and dies when you log out there or change the password. It also stops the password crossing the network on every server start.

## Consequences

- A login every time a session expires: about once a year at nemlig. Bilka's `login_token` lifetime is set by the store; we check it live.
- A dead nemlig cookie gives an anonymous basket, not a 401. With a saved session the store checks that every basket it reads has an email, also before it changes quantities, so it never shops anonymously by mistake.
- A Gigya rate limit or server error is "try again later", not an expired session.
- Old entries with a password still work. After the next login with them, the store replaces the password with the session (`stores.save_session`).
- Env vars `HANDLE_<STORE>_EMAIL`/`_PASSWORD` still hold a password. They are the human's own choice; nothing is saved from them except by `--from-env`, which saves the session.
