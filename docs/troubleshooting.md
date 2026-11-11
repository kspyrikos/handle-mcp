# Troubleshooting

Store errors are returned to the agent as a short English message. One store failing never stops the others.

| Message | Fix |
|---|---|
| `No login saved for <store>` | Run `handle-mcp login <store>` in a terminal. |
| `Login was rejected. Check your email and password.` | Check your credentials and run `handle-mcp login <store>` again. |
| `nemlig.com currently has a queue (queue-it)` | nemlig.com has a queue. Try again in a few minutes. |
| `The cart is set to pickup` (BilkaToGo) | Choose home delivery (not pickup) once on bilkatogo.dk. |
| `unexpected error (...)` | The store's API may have changed. The log on stderr has the details. Please open an issue. |
