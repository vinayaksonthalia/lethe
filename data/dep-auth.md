# Service dependency — Auth

The AuthService stores all session data in the eu-west-redis cache and cannot serve logins if it is unavailable.
