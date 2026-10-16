# Local GitHub App key (development only)

`app-dev-private-key.pem` signs GitHub App JWTs in the local stack (vault service) and
`app-dev-public-key.pem` lets the GitHub stand-in in `fakes` verify them. The pair is valid only
against the stand-in; it is not and must never be registered with a real GitHub App. Staging and
production use the real app's private key, entered as the `GITHUB_APP_PRIVATE_KEY` secret.
