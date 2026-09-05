# Client-side cryptography review checklist

Use this reference when reviewing browser-only encryption/decryption tools, especially Vite/JWE/AES-GCM apps with optional companion CLI utilities.

## High-value security checks

1. **DOM sinks from local or encrypted metadata**
   - Treat local filenames, decrypted text, JWE protected headers, QR payload labels, and parsed metadata as attacker-controlled.
   - Flag `innerHTML` / template strings that include `File.name` or unauthenticated header fields. Prefer `textContent` and explicit DOM node construction.
   - Parsing a JWE protected header does not authenticate it; header values are safe only for display after output encoding, not direct HTML.

2. **KDF parameter denial-of-service**
   - Encryption paths often validate minimum KDF settings; decrypt paths must also validate maximum and type/range bounds before running Argon2/PBKDF2.
   - Check Argon2 `m`, `t`, `p` and PBES2 `p2c` from ciphertext headers. Malicious values can freeze the browser, exhaust memory, or lock a worker.
   - Companion CLIs may already enforce bounds; compare and call out mismatches with Web code.

3. **Cross-platform parameter policy**
   - For interoperability claims, compare README, HTML input min/max, JS constants, Web runtime validation, and Python/CLI validation.
   - Inconsistent PBES2/Argon2 limits can let one client generate ciphertext another official client refuses to decrypt.

4. **Worker reliability**
   - For crypto workers, verify `onmessage` is not the only completion path.
   - Look for `onerror`, `onmessageerror`, timeout, pending-promise cleanup, and worker termination/recreation strategy.

5. **Privacy posture vs external requests**
   - Privacy-first/zero-knowledge tools should minimize third-party loads such as hosted fonts.
   - Check for CSP or deployment guidance that constrains scripts, workers, styles, and network access.

6. **Batch encryption key/salt reuse**
   - In password-derived encryption, inspect whether batch mode derives one key/salt for all files.
   - Reuse may be a deliberate performance tradeoff, but reviewers should flag it for threat-model clarification and recommend per-file salt/key when feasible.

## Testing expectations

- Round-trip tests for each supported KDF/mode.
- Tampering tests for header, IV, ciphertext, and authentication tag.
- Wrong-password failure tests.
- Regression tests that attacker-controlled filenames and header fields render as text, not HTML.
- KDF bound rejection tests for maliciously large parameters.
- Cross-language fixture tests when the project claims Web/CLI interoperability.

## Vitest browser-compat pitfall

If a Vitest/jsdom test imports Node built-ins such as `util` or `buffer` and Vite externalizes them with browser-compat warnings, the test suite can fail before collecting tests. In review reports, distinguish this test harness failure from production behavior, and recommend using Vitest's Node environment for such tests, jsdom-compatible polyfills, or avoiding Node built-ins in browser-targeted tests.
