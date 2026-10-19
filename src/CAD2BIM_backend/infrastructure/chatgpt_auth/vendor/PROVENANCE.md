# Official authentication source

Source: https://github.com/openai/sign-in-with-chatgpt-devkit

Commit: `f723814abdccec135b519c451fb6e1992ee5e933` (`@siwc/local 0.1.0`).
The seven files in `local/` are from `packages/local/src/`;
`credential-encryption.ts` is from `examples/paste-perfect/electron/`.
The original license and notice are retained. No UI/product code is included.

Project extension (not an existing official API): `ChatGPTClient.getAccessToken`
returns `authenticated(async (accessToken) => accessToken, options.signal)`.
Only `local/types.ts` and `local/index.ts` change. All OAuth, OIDC validation,
refresh/rotation, host ID, locking and encrypted persistence remain official-owned.
The safeStorage adapter is unchanged. Model calls use the Python SDK directly.
