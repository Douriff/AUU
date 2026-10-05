# Account security: optional TOTP 2FA, recovery codes, login records (P1-4)

- Off by default for every account; an account without 2FA logs in exactly as before (one step, cookie).
- Enable (Settings → 账户安全): current password → QR (PNG data-URI, segno, quiet-zone border=4 for dark UI / Google Authenticator) + secret → one 6-digit code verifies → 10 one-time
  recovery codes shown once. Stored: secret Fernet-encrypted (key = HKDF(AUU_SESSION_SECRET)), recovery codes SHA-256 only.
  Rotating `AUU_SESSION_SECRET` makes stored TOTP secrets unreadable: users then log in with a recovery code and re-bind.
- Login with 2FA: `POST /auth/login` → `{totp_required, ticket}` (5 min, HMAC, bound to session_epoch, no cookie) →
  `POST /auth/login/totp {ticket, code}` (TOTP ±30 s, each step once; or a recovery code, once). Failures count toward the
  existing per-account lockout and per-IP rate limit.
- Re-verification: disabling 2FA / regenerating recovery codes need password + code; changing the password needs the
  current password (+ code when 2FA is on). A password change now ends other sessions (session_epoch bump; this device is re-stamped).
- `GET /auth/security`: 2FA state, recovery codes left, last 20 login records (time, IP, UA, result) from `auth_log.sqlite`
  (90 days, 200 per user). `POST /auth/sessions/revoke-others`: ends every other session.
- Libraries: pyotp 2.9.0 (MIT), segno 1.6.6 (BSD), cryptography (already installed).
