# Optional session authentication

Installed only when the frozen design selects `auth-session-v1` and links approved feature IDs.
The baseline template remains anonymous. Installation is not evidence of accepted product behavior.

- `POST /api/v1/auth/login` accepts `username` and `password` and returns an opaque bearer token.
- `GET /api/v1/auth/me` requires `Authorization: Bearer <token>` and returns only ID and username.
- `POST /api/v1/auth/logout` revokes only the presented session; it is idempotent.
- Passwords use salted PBKDF2-SHA256; sessions store only token hashes, expire after eight hours,
  and inactive users cannot authenticate. Five failed attempts lock the account for five minutes.
- There is no default account, public signup, password reset or email service. Implement only the
  provisioning or registration flow approved for the application, using `create_user(db, ...)`
  and committing the surrounding transaction. Never publish real or fixed production credentials.
- Wrap the approved authenticated UI in `AuthProvider`; `useAuth()` exposes `user`, `login`,
  `logout` and `request`. Tokens stay in memory, so a page refresh requires sign-in. No auth cookie
  or URL token is accepted. Deploy over HTTPS; browser CORS alone is not an authorization boundary.
- Protected backend routes use `Depends(require_user)`. Filter private business records by that
  user's ID in the backend; authenticated identity does not imply permission to any other record.
- Migration `0002_auth_session` follows the baseline migration. The installer refuses custom
  migration histories and existing destination files instead of overwriting business code.
- Add approved UI, provisioning, ownership rules and smoke/acceptance journeys. Run complete
  integration checks after installation. This module does not itself create a completed work item.
