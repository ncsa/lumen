# Introduction

## What is Lumen?

Lumen is an AI model gateway for research institutions. It sits in front of many different AI models — hosted on different servers and platforms across your institution — and gives you a single, unified way to reach all of them.

You can interact with those models through a browser-based chat interface, or point your own applications and tools at Lumen's API and access every model the same way, without needing a separate account or key for each one. A single Lumen API key is all you need.

## Tokens and Coins

Two numbers matter when you use Lumen: **tokens** and **coins**.

### Tokens

A **token** is the basic unit of text that an AI model processes. Roughly speaking, one token is about four English characters or three-quarters of a word. The sentence "Explain quantum computing in plain terms" is about 8 tokens.

Every AI request involves two kinds of tokens:

| Kind | What it counts |
|------|---------------|
| **Input tokens** | Everything you send — your message, any files, and the conversation history |
| **Output tokens** | The model's reply |

Tokens are how the AI industry measures usage. Lumen tracks them so you can see how much of each model you are consuming.

### Coins

**Coins** are Lumen's internal currency. They exist so that one budget can cover many different models — each with different real-world prices — without you having to think in dollars and cents.

Each model has a published rate in coins per million tokens (shown separately for input and output). When you send a message, the coin cost is calculated like this:

```
cost = (input_tokens / 1,000,000 × input_rate)
     + (output_tokens / 1,000,000 × output_rate)
```

Your **coin pool** is your budget. As you use models, coins are deducted from that pool. If your institution auto-refills pools, the balance tops up on a regular schedule.

**Example:** Ask a model a simple question — the model might use ~1,000 input tokens and ~300 output tokens. If the rates are 0.5 coins per 1M input and 1.0 coins per 1M output, the cost is:

```
(1,000 / 1,000,000 × 0.5) + (300 / 1,000,000 × 1.0)
= 0.0005 + 0.0003
= 0.0008 coins
```

You'd need over 1,000 similar questions to spend 1 coin.

**In practice:** you rarely need to think about the math. The Usage page shows your balance and burn rate, and the chat interface will warn you before you run out.

## What You Can Do with Lumen

| Feature | Where |
|---------|-------|
| Chat with AI models in a browser | [Chat](guides/chat.md) |
| Track your token and coin usage | [Usage](guides/usage.md) |
| Create API keys for programmatic access | [Usage → API Keys](guides/usage.md#api-keys) |
| Use Lumen from your own code or tools | [API Reference](guides/api-reference.md) |
| Browse available models and their capabilities | [Models](models/models.md) |
| Manage application clients (managers/admins) | [Clients](clients/clients.md) |
| Configure Lumen (admins only) | [Admin Guide](admin/config.md) |
